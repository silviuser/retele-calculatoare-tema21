FROM python:3.11-slim
WORKDIR /app
COPY server.py .
EXPOSE 5000
# -u: output nebufferat, ca log-urile sa apara imediat in `docker compose logs`
CMD ["python", "-u", "server.py"]
