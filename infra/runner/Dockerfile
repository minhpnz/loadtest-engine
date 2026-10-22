FROM ghcr.io/actions/actions-runner:2.338.0
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git make python3 python3-pip python3-venv unzip \
    && rm -rf /var/lib/apt/lists/*
ARG AWSCLI_VERSION=2.38.3
RUN curl -fsSLo /tmp/awscliv2.zip "https://awscli.amazonaws.com/awscli-exe-linux-x86_64-${AWSCLI_VERSION}.zip" \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip
ARG KUBECTL_VERSION=v1.36.0
RUN curl -fsSLo /tmp/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && curl -fsSLo /tmp/kubectl.sha256 "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256" \
    && cd /tmp && echo "$(cat kubectl.sha256)  kubectl" | sha256sum --check \
    && install -m 0755 /tmp/kubectl /usr/local/bin/kubectl \
    && rm /tmp/kubectl /tmp/kubectl.sha256
USER runner
WORKDIR /home/runner
