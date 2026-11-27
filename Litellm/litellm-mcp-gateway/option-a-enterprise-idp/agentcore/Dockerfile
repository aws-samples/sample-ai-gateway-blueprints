# AgentCore Runtime runs ARM64 containers.
# The base image carries Debian package CVEs (an ECR basic scan of this build found high and
# medium findings, no critical, all in base packages). For production, add
# "RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*" after FROM, or use a
# minimal base image, and turn on Amazon Inspector enhanced scanning for the repository.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim-bookworm
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Trust the private CA of the gateway certificate: public roots (certifi) plus the private CA.
# The runtime sets SSL_CERT_FILE=/app/ca-bundle.pem (create-runtime.json), which both litellm and
# httpx read.
COPY private-ca.pem /app/private-ca.pem
RUN cat "$(python -c 'import certifi; print(certifi.where())')" /app/private-ca.pem > /app/ca-bundle.pem
COPY agent.py ./
RUN useradd -r -u 1000 agent
USER 1000
EXPOSE 8080
CMD ["python", "agent.py"]
