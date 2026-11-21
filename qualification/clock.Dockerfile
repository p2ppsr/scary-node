ARG NODE_IMAGE=scary-node:local
FROM ${NODE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends libfaketime \
    && ln -s "$(dpkg-query -L libfaketime | sed -n '/\/libfaketime\.so\.1$/p')" /usr/local/lib/qualification-clock.so \
    && rm -rf /var/lib/apt/lists/*
