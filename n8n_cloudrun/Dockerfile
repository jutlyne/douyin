# Current official Node line matching upstream n8n's Node runtime.
FROM node:24.18.0-alpine3.24

USER root
WORKDIR /opt/n8n

RUN apk upgrade --no-cache

# Resolve a lockfile without installing packages, fail on any known npm
# vulnerability, then install the audited production dependency graph.
RUN npm init -y \
    && npm install --package-lock-only --ignore-scripts --save-exact n8n@2.33.7 \
    && npm audit fix --package-lock-only --omit=dev \
    && npm audit --omit=dev --audit-level=low \
    && npm ci --omit=dev \
    && npm cache clean --force \
    && ln -s /opt/n8n/node_modules/.bin/n8n /usr/local/bin/n8n \
    && mkdir -p /home/node/.n8n \
    && chown -R node:node /home/node /opt/n8n

ENV NODE_ENV=production \
    N8N_RELEASE_TYPE=stable \
    N8N_ENDPOINT_HEALTH=health \
    SHELL=/bin/sh

WORKDIR /home/node
USER node
EXPOSE 5678/tcp
ENTRYPOINT ["n8n"]
