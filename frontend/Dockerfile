# Frontend container (E0.1). Fully decoupled from the API by decision D2:
# the API never serves these assets in any environment. Dev target runs the
# Vite dev server; prod target is a static nginx build (CDN-ready for E8).

FROM node:20-slim AS deps
WORKDIR /srv/frontend
COPY package.json package-lock.json ./
RUN npm ci

FROM deps AS dev
COPY . .
EXPOSE 5173
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:5173/').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
CMD ["npx", "vite", "--host", "0.0.0.0", "--port", "5173"]

FROM deps AS build
COPY . .
RUN npm run build

FROM nginx:alpine AS prod
COPY --from=build /srv/frontend/dist /usr/share/nginx/html
EXPOSE 80
