# 1) Build du frontend React (Vite)
FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY index.html vite.config.js ./
COPY src ./src
RUN npm run build

# 2) Caddy sert le build et relaie /api et /ws vers l'API dashboard
FROM caddy:2-alpine
# L'image donne à caddy la capacité « bind < 1024 » ; inutile ici (écoute sur 8443) et refusée
# par cap_drop: [ALL] + no-new-privileges : on la retire.
RUN setcap -r /usr/bin/caddy
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
EXPOSE 8443
