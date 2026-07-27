# syntax=docker/dockerfile:1
#
# Gideon frontend (Next.js 15, React 19, Tailwind v4).
# Multi-stage: install deps, build, then serve the standalone output.

FROM node:20-alpine AS dependencies
RUN apk add --no-cache libc6-compat
WORKDIR /usr/app

COPY package.json package-lock.json* yarn.lock* ./
RUN if [ -f package-lock.json ]; then npm ci; \
    elif [ -f yarn.lock ]; then yarn install --frozen-lockfile --network-timeout 1000000; \
    else npm install; fi

# ----- build -----
FROM dependencies AS build_image
COPY . .

# NEXT_PUBLIC_* values are inlined at build time, so they must be present here.
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_APP_URL
ARG NEXT_PUBLIC_GOOGLE_CLIENT_ID
ARG NEXT_PUBLIC_AUTH_API_URL
ARG NEXT_PUBLIC_ADMIN_EMAILS
ARG NEXT_PUBLIC_WEATHER_VIDEO_BASE

ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_PUBLIC_APP_URL=$NEXT_PUBLIC_APP_URL \
    NEXT_PUBLIC_GOOGLE_CLIENT_ID=$NEXT_PUBLIC_GOOGLE_CLIENT_ID \
    NEXT_PUBLIC_AUTH_API_URL=$NEXT_PUBLIC_AUTH_API_URL \
    NEXT_PUBLIC_ADMIN_EMAILS=$NEXT_PUBLIC_ADMIN_EMAILS \
    NEXT_PUBLIC_WEATHER_VIDEO_BASE=$NEXT_PUBLIC_WEATHER_VIDEO_BASE \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

RUN npm run build

# ----- runtime -----
FROM node:20-alpine AS runtime
RUN apk add --no-cache libc6-compat && \
    addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

WORKDIR /usr/app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME="0.0.0.0"

# `output: "standalone"` in next.config.ts bundles runtime deps into
# .next/standalone, so the runtime image needs no dependency install.
COPY --from=build_image --chown=nextjs:nodejs /usr/app/.next/standalone ./
COPY --from=build_image --chown=nextjs:nodejs /usr/app/.next/static ./.next/static
COPY --from=build_image --chown=nextjs:nodejs /usr/app/public ./public

USER nextjs
EXPOSE 3000

CMD ["node", "server.js"]
