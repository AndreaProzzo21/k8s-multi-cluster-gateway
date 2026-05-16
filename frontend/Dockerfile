FROM nginx:alpine
# Copia la config del proxy
COPY nginx.conf /etc/nginx/conf.d/default.conf
# Copia i file statici
COPY . /usr/share/nginx/html
EXPOSE 80