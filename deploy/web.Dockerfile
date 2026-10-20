# Builds trade/web (context) into static files and serves them with Caddy. The Caddyfile is mounted by compose.
FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM caddy:2-alpine
COPY --from=build /app/dist /srv
