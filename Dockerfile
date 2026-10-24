FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt requirements-production.txt ./
RUN pip install --no-cache-dir -r requirements-production.txt
RUN useradd --create-home --uid 10001 escala && mkdir /data && chown escala:escala /data
COPY --chown=escala:escala . .
USER escala
RUN python -c "import os,secrets; os.environ['DJANGO_DEBUG']='0'; os.environ['DJANGO_SECRET_KEY']=secrets.token_urlsafe(48); os.environ['DJANGO_SETTINGS_MODULE']='config.settings'; from django.core.management import execute_from_command_line; execute_from_command_line(['manage.py','collectstatic','--noinput'])"
ENV DJANGO_DEBUG=0 DATABASE_PATH=/data/db.sqlite3
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--access-logfile", "-", "--error-logfile", "-"]
