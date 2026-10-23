FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 jt \
    && useradd --uid 10001 --gid 10001 --no-create-home jt \
    && mkdir /data && chown 10001:10001 /data
COPY --chown=10001:10001 jt_community ./jt_community
COPY --chown=10001:10001 static ./static
COPY --chown=10001:10001 LICENSE THIRD-PARTY.md ./
USER 10001:10001
EXPOSE 8189
CMD ["python", "-m", "jt_community.app"]
