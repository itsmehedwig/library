from .models import SystemSettings

def system_settings(request):
    """
    Make system settings available to all templates
    """
    settings = SystemSettings.get_settings()
    return {
        'system_settings': settings
    }
