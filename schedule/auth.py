from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied
from .identity import is_account_enabled

class EmailOrUsernameBackend(ModelBackend):
    def authenticate(self,request,username=None,password=None,**kwargs):
        if username is None or password is None:return None
        User=get_user_model();identity=username.strip()
        if '@' in identity:
            candidates=list(User.objects.filter(username__iexact=identity)[:2])
            if not candidates:candidates=list(User.objects.filter(email__iexact=identity)[:2])
        else:candidates=list(User.objects.filter(username=identity)[:2])
        if len(candidates)!=1:
            User().set_password(password)
            if '@' in identity:raise PermissionDenied
            return None
        user=candidates[0]
        if user.check_password(password):
            if is_account_enabled(user):return user
            # Stop Django's legacy backend from accepting an enabled alias
            # whose resident is disabled, or a source with a separate login.
            raise PermissionDenied
        if '@' in identity:raise PermissionDenied

    def get_user(self,user_id):
        user=super().get_user(user_id)
        return user if user and is_account_enabled(user) else None
