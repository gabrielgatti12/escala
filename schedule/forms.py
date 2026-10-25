from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django import forms
from .identity import operational_residents

class ResidentAccountForm(UserCreationForm):
    resident=forms.ModelChoiceField(queryset=get_user_model().objects.none(),label='Residente da planilha',empty_label='Selecione o residente')
    field_order=['resident','username','password1','password2']
    class Meta(UserCreationForm.Meta):
        model=get_user_model()
        fields=['username']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['resident'].queryset=operational_residents().filter(is_active=True,password__startswith='!',access_account__isnull=True).order_by('first_name','username')
        self.fields['resident'].label_from_instance=lambda user:user.get_full_name() or user.username
        self.fields['username'].label='Usuário de acesso'
        self.fields['username'].help_text='Escolha um usuário simples, como nome.sobrenome. O nome importado da planilha será preservado.'
