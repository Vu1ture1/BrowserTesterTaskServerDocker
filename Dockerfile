FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends apache2 apache2-dev gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mod_wsgi-express module-config > /etc/apache2/mods-available/wsgi.load \
    && a2enmod wsgi \
    && echo "ServerName localhost" > /etc/apache2/conf-available/servername.conf \
    && a2enconf servername \
    && ln -sf /dev/stderr /var/log/apache2/error.log \
    && ln -sf /dev/stdout /var/log/apache2/access.log \
    && ln -sf /dev/stdout /var/log/apache2/other_vhosts_access.log

COPY apache/video_service.conf /etc/apache2/sites-available/video_service.conf
RUN a2dissite 000-default && a2ensite video_service

COPY web_service.py web_service.wsgi ./

RUN mkdir -p /app/uploads && chown www-data:www-data /app/uploads

EXPOSE 80
CMD ["apache2ctl", "-D", "FOREGROUND"]
