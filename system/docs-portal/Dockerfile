# OrcheStack in-package auth + setup container.
#
# This container runs on every customer's host as part of `docker compose up
# OrcheStack`. It serves ONLY the screens that must exist locally on the install:
#
#   1. /signup — first-admin bootstrap
#   2. /login  — admin auth
#   3. /setup/* — 4-step onboarding wizard (welcome → select → configure → deploying)
#   4. /assets/* — shared CSS
#
# The public marketing site (landing page, services catalog, docs, contact) is
# NOT in this image. It lives in ../../website/ and is deployed separately to
# an external host (Vercel / Netlify / a static-site VPS) on the OrcheStack team's
# own domain. This is deliberate:
#
#   - The marketing site needs SEO and discoverability — it must be indexable
#     by Google, which means it has to live at a stable public URL, not on a
#     random customer's localhost.
#   - Keeping marketing out of the image means a lighter image that customers
#     pull every time they install. They don't need to ship your website.
#   - Brand protection still applies to the setup flow: content here is BAKED
#     into the image at build time (no volume mounts). Operators cannot silently
#     re-brand the onboarding wizard without forking and rebuilding.
#
# If a customer wants to fully white-label, they fork this repo, swap the
# assets under `public/`, and build their own image with their own tag.

FROM nginx:stable-alpine3.23-perl

# Remove the default nginx site config so ours takes over.
RUN rm /etc/nginx/conf.d/default.conf

# Copy the site configuration (scoped to /signup, /login, /setup/*, /assets/*).
COPY nginx.conf /etc/nginx/conf.d/OrcheStack.conf

# Copy the in-package pages into the image.
# `public/` contains only:
#   - signup.html, login.html
#   - setup/welcome.html, setup/select.html, setup/configure.html, setup/deploying.html
#   - assets/css/main.css
COPY public/ /usr/share/nginx/html/

# Container health check — nginx must respond on :80.
# We probe /login because / is intentionally a 404 on this container.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget --quiet --spider http://localhost/login || exit 1

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
