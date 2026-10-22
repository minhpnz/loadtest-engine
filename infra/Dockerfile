FROM alpine/k8s:1.37.0@sha256:b421c2e9419edb98db39b6ab641669f4db7bb2acf354f22450c6b7e7176d1ff4
RUN apk add --no-cache python3 py3-yaml aws-cli && adduser -D -u 10001 engine
WORKDIR /platform
COPY infra/ /platform/infra/
ENV PYTHONDONTWRITEBYTECODE=1
USER engine
ENTRYPOINT ["python3", "-m", "infra.engine"]
