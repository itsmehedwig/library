from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from django.db.models import Q
from django.db import transaction
from datetime import timedelta
import csv
from io import TextIOWrapper

from .models import User, Student, Book, Transaction, VerificationCode, TransactionItem, Librarian, SystemSettings, AdminLog
from .forms import (LoginForm, StudentIDVerificationForm, StudentRegistrationForm,
                   EmailVerificationForm, CSVUploadForm, BookForm, POSUserForm,
                   StudentSearchForm, ISBNSearchForm, TransactionCodeForm, StudentForm,
                   LibrarianForm, SystemSettingsForm)


def user_login(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    
    if request.method == 'POST':
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(request, username=username, password=password)
            
            if user is not None:
                login(request, user)
                if user.user_type == 'admin':
                    return redirect('admin_dashboard')
                elif user.user_type == 'librarian':
                    return redirect('librarian_dashboard')
                elif user.user_type == 'pos':
                    return redirect('pos_home')
                else:
                    return redirect('student_dashboard')
            else:
                messages.error(request, 'Invalid username or password')
    else:
        form = LoginForm()
    
    return render(request, 'library/login.html', {'form': form})


def user_logout(request):
    logout(request)
    return redirect('login')


def verify_student_id(request):
    if request.method == 'POST':
        form = StudentIDVerificationForm(request.POST)
        if form.is_valid():
            student_id = form.cleaned_data['student_id']
            try:
                student = Student.objects.get(student_id=student_id)
                if student.user is not None:
                    messages.error(request, 'This student ID is already registered')
                    return redirect('verify_student_id')
                request.session['student_id'] = student_id
                return redirect('student_registration')
            except Student.DoesNotExist:
                messages.error(request, 'Student ID not found in the system. Please contact the admin.')
    else:
        form = StudentIDVerificationForm()
    
    return render(request, 'library/verify_student_id.html', {'form': form})


def student_registration(request):
    student_id = request.session.get('student_id')
    if not student_id:
        return redirect('verify_student_id')
    
    student = get_object_or_404(Student, student_id=student_id)
    
    if request.method == 'POST':
        form = StudentRegistrationForm(request.POST, request.FILES, instance=student)
        if form.is_valid():
            email = form.cleaned_data['email']
            password = form.cleaned_data['password']
            
            try:
                with transaction.atomic():
                    student = form.save(commit=False)
                    student.save()
                    
                    user, created = User.objects.get_or_create(
                        username=student_id,
                        defaults={
                            'email': email,
                            'user_type': 'student',
                            'is_active': False
                        }
                    )
                    
                    if created:
                        user.set_password(password)
                        user.save()
                    
                    student.user = user
                    student.save()
                    
                del request.session['student_id']
                messages.success(request, 'Registration successful! Your account is pending admin approval. You will be able to login once approved.')
                return redirect('login')
                
            except Exception as e:
                messages.error(request, f'Registration failed: {str(e)}. Please try again.')
                return render(request, 'library/student_registration.html', {
                    'form': form,
                    'student': student
                })
    else:
        form = StudentRegistrationForm(instance=student)
    
    return render(request, 'library/student_registration.html', {
        'form': form,
        'student': student
    })


def email_verification(request):
    student_id = request.session.get('student_id_for_verification')
    if not student_id:
        return redirect('verify_student_id')
    
    student = get_object_or_404(Student, student_id=student_id)
    
    if request.method == 'POST':
        form = EmailVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data['code']
            try:
                verification = VerificationCode.objects.get(
                    student=student,
                    code=code,
                    is_used=False
                )
                if verification.is_valid():
                    verification.is_used = True
                    verification.save()
                    student.is_verified = True
                    student.save()
                    
                    del request.session['student_id']
                    del request.session['student_id_for_verification']
                    
                    messages.success(request, 'Email verified successfully! You can now login.')
                    return redirect('login')
                else:
                    messages.error(request, 'Verification code has expired. Please request a new one.')
            except VerificationCode.DoesNotExist:
                messages.error(request, 'Invalid verification code')
    else:
        form = EmailVerificationForm()
    
    return render(request, 'library/email_verification.html', {'form': form})


@login_required
def student_dashboard(request):
    if request.user.user_type != 'student':
        return redirect('dashboard')
    
    student = Student.objects.get(user=request.user)
    borrowed_books = Transaction.objects.filter(
        student=student,
        status='borrowed',
        approval_status='approved'
    ).prefetch_related('items__book')
    
    history = Transaction.objects.filter(
        student=student,
        approval_status='approved'
    ).prefetch_related('items__book').order_by('-borrowed_date')[:10]
    
    search_query = request.GET.get('search', '')
    category = request.GET.get('category', '')
    
    books = Book.objects.all()
    if search_query:
        books = books.filter(
            Q(title__icontains=search_query) |
            Q(author__icontains=search_query) |
            Q(isbn__icontains=search_query)
        )
    if category:
        books = books.filter(category=category)
    
    categories = Book.objects.values_list('category', flat=True).distinct().order_by('category')
    
    returned_count = Transaction.objects.filter(
        student=student,
        status='returned',
        approval_status='approved'
    ).count()
    
    context = {
        'student': student,
        'borrowed_books': borrowed_books,
        'history': history,
        'books': books,
        'categories': categories,
        'search_query': search_query,
        'selected_category': category,
        'returned_count': returned_count,
    }
    
    return render(request, 'library/student_dashboard.html', context)


@login_required
def admin_dashboard(request):
    if request.user.user_type != 'admin':
        return redirect('dashboard')
    
    from .models import TransactionItem
    from django.db.models import Sum
    
    total_students = Student.objects.count()
    
    total_book_copies = Book.objects.aggregate(total=Sum('copies_total'))['total'] or 0
    total_borrowed = TransactionItem.objects.filter(
        status='borrowed',
        transaction__approval_status='approved'
    ).count()
    total_available = total_book_copies - total_borrowed
    
    total_books = Book.objects.count()
    pending_registrations = Student.objects.filter(
        user__isnull=False,
        is_approved=False
    ).count()
    pending_borrowing = Transaction.objects.filter(
        approval_status='pending'
    ).count()
    
    # 🔧 Updated: exclude returned transactions
    recent_transactions = Transaction.objects.filter(
        approval_status='approved'
    ).exclude(
        status='returned'
    ).select_related(
        'student'
    ).prefetch_related(
        'items__book'
    ).order_by('-borrowed_date')[:10]
    
    context = {
        'total_students': total_students,
        'total_books': total_books,
        'total_borrowed': total_borrowed,
        'total_available': total_available,
        'pending_registrations': pending_registrations,
        'pending_borrowing': pending_borrowing,
        'recent_transactions': recent_transactions
    }
    
    return render(request, 'library/admin_dashboard.html', context)


@login_required
def import_books_csv(request):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    if request.method == 'POST':
        form = CSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                csv_file = request.FILES['csv_file']
                decoded_file = TextIOWrapper(csv_file.file, encoding='utf-8-sig')
                reader = csv.DictReader(decoded_file)
                
                success_count = 0
                error_count = 0
                errors_list = []
                
                for row_num, row in enumerate(reader, start=2):
                    try:
                        isbn = row.get('ISBN', row.get('isbn', '')).strip()
                        title = row.get('Book Name', row.get('Book name', row.get('title', ''))).strip()
                        author = row.get('Author', row.get('author', '')).strip()
                        category = row.get('Category', row.get('category', '')).strip()
                        publisher = row.get('Publisher', row.get('publisher', '')).strip()
                        year_published = row.get('Date Published', row.get('Date published', row.get('year_published', ''))).strip()
                        copies_total = row.get('Pieces', row.get('pieces', row.get('copies_total', '1'))).strip()
                        description = row.get('Description', row.get('description', '')).strip()
                        
                        if not isbn:
                            error_count += 1
                            errors_list.append(f'Row {row_num}: Missing ISBN')
                            continue
                        if not title:
                            error_count += 1
                            errors_list.append(f'Row {row_num}: Missing Book Name')
                            continue
                        if not author:
                            error_count += 1
                            errors_list.append(f'Row {row_num}: Missing Author')
                            continue
                        if not category:
                            error_count += 1
                            errors_list.append(f'Row {row_num}: Missing Category')
                            continue
                        
                        copies_num = 1
                        if copies_total:
                            if not copies_total.isdigit() or int(copies_total) < 1:
                                error_count += 1
                                errors_list.append(f'Row {row_num}: Invalid Pieces (must be positive integer)')
                                continue
                            copies_num = int(copies_total)
                        
                        year_num = None
                        if year_published:
                            if not year_published.isdigit():
                                error_count += 1
                                errors_list.append(f'Row {row_num}: Invalid Date Published (must be a number)')
                                continue
                            year_num = int(year_published)
                            if year_num < 1000 or year_num > 9999:
                                error_count += 1
                                errors_list.append(f'Row {row_num}: Invalid Date Published (must be 4 digits)')
                                continue
                        
                        book, created = Book.objects.get_or_create(
                            isbn=isbn,
                            defaults={
                                'title': title,
                                'author': author,
                                'category': category,
                                'publisher': publisher,
                                'year_published': year_num,
                                'copies_total': copies_num,
                                'copies_available': copies_num,
                                'description': description
                            }
                        )
                        if created:
                            success_count += 1
                            if request.user.user_type == 'librarian':
                                AdminLog.objects.create(
                                    librarian=request.user,
                                    action='book_import',
                                    description=f'Imported book: {title} (ISBN: {isbn})'
                                )
                        else:
                            error_count += 1
                            errors_list.append(f'Row {row_num}: Book with ISBN {isbn} already exists')
                    except Exception as e:
                        error_count += 1
                        errors_list.append(f'Row {row_num}: {str(e)}')
                
                if errors_list and len(errors_list) <= 10:
                    for error in errors_list:
                        messages.warning(request, error)
                elif errors_list:
                    messages.warning(request, f'Showing first 10 of {len(errors_list)} errors. Please check your CSV file.')
                    for error in errors_list[:10]:
                        messages.warning(request, error)
                
                messages.success(request, f'Successfully imported {success_count} books. {error_count} errors.')
                return redirect('manage_books')
            except KeyError as e:
                expected_format = 'ISBN, Book Name, Author, Date Published, Category, Pieces, Description'
                messages.error(request, f'Error: Missing required column in CSV file. Expected format: {expected_format}')
            except Exception as e:
                expected_format = 'ISBN, Book Name, Author, Date Published, Category, Pieces, Description'
                messages.error(request, f'Error processing CSV file: {str(e)}. Expected format: {expected_format}')
        else:
            messages.error(request, 'Invalid form submission. Please upload a valid CSV file.')
    else:
        form = CSVUploadForm()
    
    return render(request, 'library/import_books_csv.html', {'form': form})


@login_required
def import_students_csv(request):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    if request.method == 'POST':
        form = CSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                csv_file = request.FILES['csv_file']
                decoded_file = TextIOWrapper(csv_file.file, encoding='utf-8-sig')
                reader = csv.DictReader(decoded_file)
                
                success_count = 0
                error_count = 0
                errors_list = []
                
                for row_num, row in enumerate(reader, start=2):
                    try:
                        student_id = row.get('Student ID', row.get('student_id', '')).strip()
                        last_name = row.get('Last Name', row.get('last_name', '')).strip()
                        first_name = row.get('First Name', row.get('first_name', '')).strip()
                        middle_name = row.get('Middle Name', row.get('middle_name', '')).strip()
                        course = row.get('Course', row.get('course', '')).strip()
                        year = row.get('Year', row.get('year', '')).strip()
                        section = row.get('Section', row.get('section', '')).strip()
                        
                        if not student_id:
                            error_count += 1
                            errors_list.append(f'Row {row_num}: Missing Student ID')
                            continue
                        if not last_name:
                            error_count += 1
                            errors_list.append(f'Row {row_num}: Missing Last Name')
                            continue
                        if not first_name:
                            error_count += 1
                            errors_list.append(f'Row {row_num}: Missing First Name')
                            continue
                        if not course:
                            error_count += 1
                            errors_list.append(f'Row {row_num}: Missing Course')
                            continue
                        if not year:
                            error_count += 1
                            errors_list.append(f'Row {row_num}: Missing Year')
                            continue
                        if not section:
                            error_count += 1
                            errors_list.append(f'Row {row_num}: Missing Section')
                            continue
                        
                        student, created = Student.objects.get_or_create(
                            student_id=student_id,
                            defaults={
                                'last_name': last_name,
                                'first_name': first_name,
                                'middle_name': middle_name,
                                'course': course,
                                'year': year,
                                'section': section
                            }
                        )
                        if created:
                            success_count += 1
                            if request.user.user_type == 'librarian':
                                AdminLog.objects.create(
                                    librarian=request.user,
                                    action='student_import',
                                    description=f'Imported student: {last_name}, {first_name} (ID: {student_id})'
                                )
                        else:
                            error_count += 1
                            errors_list.append(f'Row {row_num}: Student ID {student_id} already exists')
                    except Exception as e:
                        error_count += 1
                        errors_list.append(f'Row {row_num}: {str(e)}')
                
                if errors_list and len(errors_list) <= 10:
                    for error in errors_list:
                        messages.warning(request, error)
                elif errors_list:
                    messages.warning(request, f'Showing first 10 of {len(errors_list)} errors. Please check your CSV file.')
                    for error in errors_list[:10]:
                        messages.warning(request, error)
                
                messages.success(request, f'Successfully imported {success_count} students. {error_count} errors.')
                if request.user.user_type == 'librarian':
                    return redirect('librarian_dashboard')
                return redirect('admin_dashboard')
            except KeyError as e:
                expected_format = 'Student ID, Last Name, First Name, Middle Name, Course, Year, Section'
                messages.error(request, f'Error: Missing required column in CSV file. Expected format: {expected_format}')
            except Exception as e:
                expected_format = 'Student ID, Last Name, First Name, Middle Name, Course, Year, Section'
                messages.error(request, f'Error processing CSV file: {str(e)}. Expected format: {expected_format}')
    else:
        form = CSVUploadForm()
    
    return render(request, 'library/import_students.html', {'form': form})


@login_required
def manage_books(request):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    from django.core.paginator import Paginator
    
    books = Book.objects.all().order_by('title')
    search_query = request.GET.get('search', '')
    
    if search_query:
        books = books.filter(
            Q(title__icontains=search_query) |
            Q(author__icontains=search_query) |
            Q(isbn__icontains=search_query)
        )
    
    paginator = Paginator(books, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    return render(request, 'library/manage_books.html', {
        'books': page_obj,
        'page_obj': page_obj,
        'search_query': search_query
    })


@login_required
def add_book(request):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    if request.method == 'POST':
        form = BookForm(request.POST, request.FILES)
        if form.is_valid():
            book = form.save(commit=False)
            book.copies_available = book.copies_total
            book.save()
            messages.success(request, 'Book added successfully!')
            return redirect('manage_books')
    else:
        form = BookForm()
    
    return render(request, 'library/add_book.html', {'form': form})


@login_required
def edit_book(request, book_id):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    book = get_object_or_404(Book, id=book_id)
    
    if request.method == 'POST':
        form = BookForm(request.POST, request.FILES, instance=book)
        if form.is_valid():
            form.save()
            messages.success(request, 'Book updated successfully!')
            return redirect('manage_books')
    else:
        form = BookForm(instance=book)
    
    return render(request, 'library/edit_book.html', {'form': form, 'book': book})


@login_required
def manage_students(request):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    from django.core.paginator import Paginator
    
    pending_students = Student.objects.filter(user__isnull=False, is_approved=False).order_by('-created_at')
    
    students = Student.objects.all().order_by('last_name')
    search_query = request.GET.get('search', '')
    
    if search_query:
        students = students.filter(
            Q(student_id__icontains=search_query) |
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query)
        )
    
    paginator = Paginator(students, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    return render(request, 'library/manage_students.html', {
        'students': page_obj,
        'page_obj': page_obj,
        'pending_students': pending_students,
        'search_query': search_query
    })


@login_required
def pending_students(request):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    pending = Student.objects.filter(user__isnull=False, is_approved=False).order_by('-created_at')
    
    return render(request, 'library/pending_students.html', {
        'pending_students': pending
    })


@login_required
def approve_student(request, student_id):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    if request.method == 'POST':
        student = get_object_or_404(Student, id=student_id)
        student.is_approved = True
        student.save()
        
        if student.user:
            student.user.is_active = True
            student.user.save()
        
        messages.success(request, f'Student {student.get_full_name()} has been approved and can now login.')
        return redirect('manage_students')
    
    return redirect('manage_students')


@login_required
def reject_student(request, student_id):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    if request.method == 'POST':
        student = get_object_or_404(Student, id=student_id)
        
        if student.user:
            user = student.user
            student.user = None
            student.save()
            user.delete()
        
        messages.success(request, f'Student {student.get_full_name()} registration has been rejected.')
        return redirect('manage_students')
    
    return redirect('manage_students')


@login_required
def create_pos_account(request):
    if request.user.user_type != 'admin':
        return redirect('dashboard')
    
    if request.method == 'POST':
        form = POSUserForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']
            
            User.objects.create_user(
                username=username,
                password=password,
                user_type='pos'
            )
            messages.success(request, 'POS account created successfully!')
            return redirect('admin_dashboard')
    else:
        form = POSUserForm()
    
    return render(request, 'library/create_pos_account.html', {'form': form})


@login_required
def pos_home(request):
    if request.user.user_type != 'pos':
        return redirect('dashboard')
    
    return render(request, 'library/pos_home.html')


@login_required
def pos_borrow_book(request):
    if request.user.user_type != 'pos':
        return redirect('dashboard')
    
    if request.method == 'POST':
        if 'student_id' in request.POST:
            student_id = request.POST.get('student_id')
            try:
                student = Student.objects.get(student_id=student_id, is_approved=True)
                request.session['pos_student_id'] = student_id
                request.session['pos_books'] = []
                return render(request, 'library/pos_borrow_book.html', {
                    'student': student,
                    'step': 'add_books'
                })
            except Student.DoesNotExist:
                messages.error(request, 'Student ID not found or not approved by admin')
        
        elif 'isbn' in request.POST:
            isbn = request.POST.get('isbn')
            student_id = request.session.get('pos_student_id')
            
            if not student_id:
                return redirect('pos_borrow_book')
            
            try:
                book = Book.objects.get(isbn=isbn)
                if not book.is_available():
                    messages.error(request, 'Book is not available')
                else:
                    books = request.session.get('pos_books', [])
                    books.append({
                        'id': book.id,
                        'isbn': book.isbn,
                        'title': book.title,
                        'author': book.author
                    })
                    request.session['pos_books'] = books
                    student = Student.objects.get(student_id=student_id)
                    
                    if 'add_another' in request.POST:
                        return render(request, 'library/pos_borrow_book.html', {
                            'student': student,
                            'books': books,
                            'step': 'add_books'
                        })
                    else:
                        return render(request, 'library/pos_borrow_book.html', {
                            'student': student,
                            'books': books,
                            'step': 'confirm'
                        })
            except Book.DoesNotExist:
                messages.error(request, 'Book with this ISBN not found')
                student = Student.objects.get(student_id=student_id)
                return render(request, 'library/pos_borrow_book.html', {
                    'student': student,
                    'step': 'add_books'
                })
        
        elif 'confirm_borrow' in request.POST:
            student_id = request.session.get('pos_student_id')
            books_data = request.session.get('pos_books', [])
            
            if not student_id or not books_data:
                return redirect('pos_borrow_book')
            
            student = Student.objects.get(student_id=student_id)
            
            transaction_code = Transaction.generate_transaction_code()
            due_date = timezone.now() + timedelta(days=7)
            
            transaction = Transaction.objects.create(
                transaction_code=transaction_code,
                student=student,
                due_date=due_date,
                created_by=request.user
            )
            
            from .models import TransactionItem
            for book_data in books_data:
                book = Book.objects.get(id=book_data['id'])
                
                if book.is_available():
                    TransactionItem.objects.create(
                        transaction=transaction,
                        book=book
                    )
            
            del request.session['pos_student_id']
            del request.session['pos_books']
            
            return render(request, 'library/pos_borrow_success.html', {
                'student': student,
                'transaction': transaction
            })
    
    return render(request, 'library/pos_borrow_book.html', {'step': 'student_id'})


@login_required
def pos_return_book(request):
    if request.user.user_type != 'pos':
        return redirect('dashboard')
    
    if request.method == 'POST':
        if 'transaction_code' in request.POST:
            transaction_code = request.POST.get('transaction_code')
            transaction = Transaction.objects.filter(
                transaction_code__startswith=transaction_code,
                approval_status='approved'
            ).select_related('student').prefetch_related('items__book').first()
            
            if transaction:
                borrowed_items = transaction.items.filter(status='borrowed')
                if borrowed_items.exists():
                    return render(request, 'library/pos_return_book.html', {
                        'transaction': transaction,
                        'borrowed_items': borrowed_items,
                        'step': 'confirm'
                    })
                else:
                    messages.error(request, 'All books from this transaction have already been returned')
            else:
                messages.error(request, 'No borrowing found with this transaction code')
        
        elif 'return_books' in request.POST:
            transaction_code = request.POST.get('transaction_code_value')
            selected_items = request.POST.getlist('selected_books')
            
            if transaction_code and selected_items:
                from .models import TransactionItem
                transaction = Transaction.objects.filter(
                    transaction_code=transaction_code,
                    approval_status='approved'
                ).prefetch_related('items__book').first()
                
                if transaction:
                    returned_items = []
                    unreturned_items = []
                    
                    for item in transaction.items.filter(status='borrowed'):
                        if str(item.id) in selected_items:
                            item.status = 'returned'
                            item.return_date = timezone.now()
                            item.save()
                            
                            item.book.copies_available += 1
                            item.book.save()
                            returned_items.append(item)
                        else:
                            unreturned_items.append(item)
                    
                    all_returned = not transaction.items.filter(status='borrowed').exists()
                    if all_returned:
                        transaction.status = 'returned'
                        transaction.return_date = timezone.now()
                        transaction.save()
                    
                    return render(request, 'library/pos_return_success.html', {
                        'student': transaction.student,
                        'transaction': transaction,
                        'returned_items': returned_items,
                        'unreturned_items': unreturned_items,
                        'all_returned': all_returned
                    })
            else:
                messages.error(request, 'Please select at least one book to return')
                return redirect('pos_return_book')
    
    return render(request, 'library/pos_return_book.html', {'step': 'transaction_code'})


@login_required
def pending_transactions(request):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    pending = Transaction.objects.filter(approval_status='pending').select_related('student', 'created_by').prefetch_related('items__book').order_by('-borrowed_date')
    
    return render(request, 'library/pending_transactions.html', {
        'pending_transactions': pending
    })


@login_required
def approve_transaction(request, transaction_id):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    if request.method == 'POST':
        transaction = Transaction.objects.get(id=transaction_id)
        
        for item in transaction.items.all():
            item.book.copies_available -= 1
            item.book.save()
        
        transaction.approval_status = 'approved'
        transaction.approved_by = request.user
        transaction.approved_at = timezone.now()
        transaction.save()
        
        book_count = transaction.items.count()
        messages.success(request, f'{book_count} book(s) borrowing approved for {transaction.student.get_full_name()}')
    
    return redirect('pending_transactions')


@login_required
def reject_transaction(request, transaction_id):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    if request.method == 'POST':
        transaction = Transaction.objects.get(id=transaction_id)
        
        transaction.approval_status = 'rejected'
        transaction.approved_by = request.user
        transaction.approved_at = timezone.now()
        transaction.save()
        
        messages.success(request, f'Book borrowing request rejected')
    
    return redirect('pending_transactions')


@login_required
def dashboard(request):
    if request.user.user_type == 'admin':
        return redirect('admin_dashboard')
    elif request.user.user_type == 'librarian':
        return redirect('librarian_dashboard')
    elif request.user.user_type == 'pos':
        return redirect('pos_home')
    else:
        return redirect('student_dashboard')


@login_required
def student_settings(request):
    if request.user.user_type != 'student':
        return redirect('dashboard')
    
    student = Student.objects.get(user=request.user)
    
    if request.method == 'POST':
        student.phone_number = request.POST.get('phone_number', student.phone_number)
        
        if 'profile_photo' in request.FILES:
            student.profile_photo = request.FILES['profile_photo']
        
        email = request.POST.get('email')
        if email and email != request.user.email:
            request.user.email = email
            request.user.save()
        
        password = request.POST.get('password')
        if password:
            request.user.set_password(password)
            request.user.save()
            messages.success(request, 'Password updated. Please login again.')
            return redirect('login')
        
        student.save()
        messages.success(request, 'Settings updated successfully!')
        return redirect('student_settings')
    
    return render(request, 'library/student_settings.html', {'student': student})


@login_required
def admin_settings(request):
    if request.user.user_type != 'admin':
        return redirect('dashboard')
    
    system_settings = SystemSettings.get_settings()
    
    if request.method == 'POST':
        if 'update_system' in request.POST:
            form = SystemSettingsForm(request.POST, request.FILES, instance=system_settings)
            if form.is_valid():
                form.save()
                messages.success(request, 'System settings updated successfully!')
                return redirect('admin_settings')
        else:
            email = request.POST.get('email')
            if email:
                request.user.email = email
                request.user.save()
            
            password = request.POST.get('password')
            if password:
                request.user.set_password(password)
                request.user.save()
                messages.success(request, 'Password updated. Please login again.')
                return redirect('login')
            
            messages.success(request, 'Settings updated successfully!')
            return redirect('admin_settings')
    else:
        form = SystemSettingsForm(instance=system_settings)
    
    return render(request, 'library/admin_settings.html', {
        'form': form,
        'system_settings': system_settings
    })


@login_required
def delete_book(request, book_id):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    book = get_object_or_404(Book, id=book_id)
    
    if request.method == 'POST':
        book_title = book.title
        book.delete()
        messages.success(request, f'Book "{book_title}" deleted successfully!')
        return redirect('manage_books')
    
    return render(request, 'library/delete_book.html', {'book': book})


@login_required
def add_student(request):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    if request.method == 'POST':
        form = StudentForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Student added successfully!')
            return redirect('manage_students')
    else:
        form = StudentForm()
    
    return render(request, 'library/add_student.html', {'form': form})


@login_required
def edit_student(request, student_id):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    student = get_object_or_404(Student, id=student_id)
    
    if request.method == 'POST':
        form = StudentForm(request.POST, instance=student)
        if form.is_valid():
            form.save()
            messages.success(request, 'Student updated successfully!')
            return redirect('manage_students')
    else:
        form = StudentForm(instance=student)
    
    return render(request, 'library/edit_student.html', {'form': form, 'student': student})


@login_required
def delete_student(request, student_id):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    student = get_object_or_404(Student, id=student_id)
    
    if request.method == 'POST':
        student_name = student.get_full_name()
        if student.user:
            student.user.delete()
        student.delete()
        messages.success(request, f'Student "{student_name}" deleted successfully!')
        return redirect('manage_students')
    
    return render(request, 'library/delete_student.html', {'student': student})


@login_required
def student_books(request):
    if request.user.user_type != 'student':
        return redirect('dashboard')
    
    from django.core.paginator import Paginator
    
    search_query = request.GET.get('search', '')
    category = request.GET.get('category', '')
    
    books = Book.objects.all()
    if search_query:
        books = books.filter(
            Q(title__icontains=search_query) |
            Q(author__icontains=search_query) |
            Q(isbn__icontains=search_query)
        )
    if category:
        books = books.filter(category=category)
    
    categories = Book.objects.values_list('category', flat=True).distinct().order_by('category')
    
    paginator = Paginator(books, 12)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    student = Student.objects.get(user=request.user)
    
    context = {
        'books': page_obj,
        'page_obj': page_obj,
        'categories': categories,
        'search_query': search_query,
        'selected_category': category,
        'student': student
    }
    
    return render(request, 'library/student_books.html', context)


@login_required
def export_books_by_category(request):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    import csv
    from django.http import HttpResponse
    
    category = request.GET.get('category', '')
    
    response = HttpResponse(content_type='text/csv')
    if category:
        response['Content-Disposition'] = f'attachment; filename="books_{category}.csv"'
        books = Book.objects.filter(category=category)
    else:
        response['Content-Disposition'] = 'attachment; filename="all_books.csv"'
        books = Book.objects.all()
    
    writer = csv.writer(response)
    writer.writerow(['ISBN', 'Title', 'Author', 'Category', 'Publisher', 'Year', 'Copies Total', 'Copies Available'])
    
    for book in books:
        writer.writerow([
            book.isbn,
            book.title,
            book.author,
            book.category,
            book.publisher or '',
            book.year_published or '',
            book.copies_total,
            book.copies_available
        ])
    
    return response


@login_required
def librarian_dashboard(request):
    if request.user.user_type != 'librarian':
        return redirect('dashboard')
    
    from .models import TransactionItem
    from django.db.models import Sum
    
    total_students = Student.objects.count()
    
    total_book_copies = Book.objects.aggregate(total=Sum('copies_total'))['total'] or 0
    total_borrowed = TransactionItem.objects.filter(
        status='borrowed',
        transaction__approval_status='approved'
    ).count()
    total_available = total_book_copies - total_borrowed
    
    total_books = Book.objects.count()
    pending_registrations = Student.objects.filter(
        user__isnull=False,
        is_approved=False
    ).count()
    pending_borrowing = Transaction.objects.filter(
        approval_status='pending'
    ).count()
    
    recent_transactions = Transaction.objects.filter(
        approval_status='approved'
    ).exclude(
        status='returned'
    ).select_related(
        'student'
    ).prefetch_related(
        'items__book'
    ).order_by('-borrowed_date')[:10]
    
    context = {
        'total_students': total_students,
        'total_books': total_books,
        'total_borrowed': total_borrowed,
        'total_available': total_available,
        'pending_registrations': pending_registrations,
        'pending_borrowing': pending_borrowing,
        'recent_transactions': recent_transactions
    }
    
    return render(request, 'library/librarian_dashboard.html', context)


@login_required
def manage_librarians(request):
    if request.user.user_type != 'admin':
        return redirect('dashboard')
    
    librarians = Librarian.objects.all().select_related('user')
    return render(request, 'library/manage_librarians.html', {'librarians': librarians})


@login_required
def add_librarian(request):
    if request.user.user_type != 'admin':
        return redirect('dashboard')
    
    if request.method == 'POST':
        form = LibrarianForm(request.POST, request.FILES)
        if form.is_valid():
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']
            
            user = User.objects.create_user(
                username=username,
                password=password,
                user_type='librarian',
                email=form.cleaned_data['email']
            )
            
            librarian = form.save(commit=False)
            librarian.user = user
            librarian.save()
            
            messages.success(request, 'Librarian added successfully!')
            return redirect('manage_librarians')
    else:
        form = LibrarianForm()
    
    return render(request, 'library/add_librarian.html', {'form': form})


@login_required
def edit_librarian(request, librarian_id):
    if request.user.user_type != 'admin':
        return redirect('dashboard')
    
    librarian = get_object_or_404(Librarian, id=librarian_id)
    
    if request.method == 'POST':
        form = LibrarianForm(request.POST, request.FILES, instance=librarian)
        if form.is_valid():
            password = form.cleaned_data.get('password')
            
            if password:
                librarian.user.set_password(password)
            
            librarian.user.username = form.cleaned_data['username']
            librarian.user.email = form.cleaned_data['email']
            librarian.user.save()
            
            form.save()
            messages.success(request, 'Librarian updated successfully!')
            return redirect('manage_librarians')
    else:
        form = LibrarianForm(instance=librarian, initial={'username': librarian.user.username})
    
    return render(request, 'library/edit_librarian.html', {'form': form, 'librarian': librarian})


@login_required
def delete_librarian(request, librarian_id):
    if request.user.user_type != 'admin':
        return redirect('dashboard')
    
    librarian = get_object_or_404(Librarian, id=librarian_id)
    
    if request.method == 'POST':
        librarian_name = librarian.name
        user = librarian.user
        librarian.delete()
        user.delete()
        messages.success(request, f'Librarian "{librarian_name}" deleted successfully!')
        return redirect('manage_librarians')
    
    return render(request, 'library/delete_librarian.html', {'librarian': librarian})

@login_required
def admin_logs(request):
    if request.user.user_type != 'admin':
        return redirect('dashboard')
    
    from django.core.paginator import Paginator
    
    librarian_filter = request.GET.get('librarian', '')
    
    logs = AdminLog.objects.select_related('librarian').all()
    
    if librarian_filter:
        logs = logs.filter(librarian__id=librarian_filter)
    
    librarians = User.objects.filter(user_type='librarian')
    
    paginator = Paginator(logs, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'logs': page_obj,
        'page_obj': page_obj,
        'librarians': librarians,
        'selected_librarian': librarian_filter
    }
    
    return render(request, 'library/admin_logs.html', context)

@login_required
def student_books(request):
    if request.user.user_type != 'student':
        return redirect('dashboard')
    
    from django.core.paginator import Paginator
    
    search_query = request.GET.get('search', '')
    selected_category = request.GET.get('category', '')
    
    books = Book.objects.filter(copies_total__gt=0).order_by('title')
    
    if search_query:
        books = books.filter(
            Q(title__icontains=search_query) |
            Q(author__icontains=search_query) |
            Q(isbn__icontains=search_query)
        )
    
    if selected_category:
        books = books.filter(category=selected_category)
    
    categories = Book.objects.values_list('category', flat=True).distinct().order_by('category')
    
    paginator = Paginator(books, 24)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'books': page_obj,
        'page_obj': page_obj,
        'categories': categories,
        'search_query': search_query,
        'selected_category': selected_category,
    }
    
    return render(request, 'library/student_books.html', context)


@login_required
def download_books_csv_template(request):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    from django.http import HttpResponse
    import csv
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="books_template.csv"'
    
    writer = csv.writer(response)
    writer.writerow(['ISBN', 'Book Name', 'Author', 'Date Published', 'Category', 'Pieces', 'Description'])
    writer.writerow(['978-0-123456-78-9', 'Sample Book Title', 'John Doe', '2023', 'Fiction', '5', 'This is a sample book description'])
    
    return response


@login_required
def download_students_csv_template(request):
    if request.user.user_type not in ['admin', 'librarian']:
        return redirect('dashboard')
    
    from django.http import HttpResponse
    import csv
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="students_template.csv"'
    
    writer = csv.writer(response)
    writer.writerow(['Student ID', 'Last Name', 'First Name', 'Middle Name', 'Course', 'Year', 'Section'])
    writer.writerow(['2024-12345', 'Dela Cruz', 'Juan', 'Santos', 'BSIT', '1', 'A'])
    
    return response
