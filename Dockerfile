FROM python:3.12-slim

# Crear directorio de trabajo
WORKDIR /app

# Copiar dependencias
COPY requirements.txt .

# Instalar dependencias
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del proyecto
COPY . .

# Fly.io detecta automáticamente el puerto 8080
EXPOSE 8080

# Ejecutar el bot unificado (bot + servidor Flask)
CMD ["python3", "bot_hibrido.py"]

