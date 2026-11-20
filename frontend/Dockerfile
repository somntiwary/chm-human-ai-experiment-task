ARG NODE_BASE=public.ecr.aws/docker/library/node:22-slim
FROM ${NODE_BASE}

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.js startup-ready.mjs ./
COPY src ./src
RUN npm run build

CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--strictPort"]
