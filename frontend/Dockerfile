# PlexMigrate frontend container.
#
# Two-stage build:
#   1. `build` - node:20-alpine compiles the Vite + React + TS app
#                into static HTML/JS/CSS under /app/dist.
#   2. `serve` - nginx:alpine serves /app/dist and proxies /api + /ws
#                to the backend service.

FROM node:20-alpine AS build

WORKDIR /app

# Copy dependency manifests first so source edits don't bust the
# `npm ci` layer.
COPY package.json package-lock.json* ./

# Prefer `npm ci` (reproducible, lockfile-required). Fall back to
# `npm install` for first build before a lockfile exists.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .
RUN npm run build


FROM nginx:alpine AS serve

# Vite's default output dir.
COPY --from=build /app/dist /usr/share/nginx/html

# Replace nginx's default vhost with our SPA + proxy config.
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
