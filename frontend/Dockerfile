FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build
FROM node:24-alpine
WORKDIR /app
RUN addgroup -S appuser && adduser -S appuser -G appuser
COPY --chown=appuser:appuser --from=build /app .
USER appuser
EXPOSE 3000
CMD ["npm","run","start"]
