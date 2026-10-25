from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied

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
        if user.check_password(password) and self.user_can_authenticate(user):return user
        if '@' in identity:raise PermissionDenied
