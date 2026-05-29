# OrcheStack marketing + documentation website

This folder holds the **public, externally-hosted OrcheStack website**:

- `public/index.html` — landing page
- `public/services.html` — tool catalog
- `public/contact.html` — contact form
- `public/docs/` — 28-page documentation site (generated from `_generate_docs.py`)
- `public/assets/` — shared CSS + tool logos

It is **not** part of the Docker image that customers pull when they run
`docker compose up OrcheStack`. Customers get only the in-package auth and setup
screens from [`../system/docs-portal/`](../system/docs-portal/). This site
lives on OrcheStack's own domain, fronted by Cloudflare, so that:

1. Search engines can index it — if someone Googles "OrcheStack data platform"
   or "Nigerian open-source data stack", they land here.
2. Prospective users can read the docs before committing to an install.
3. The site can iterate on copy, SEO, and content without forcing every
   customer to pull a new Docker image.

## Where it's deployed

Default host: **https://OrcheStack.africa** (change per deployment).

The in-package auth pages (`signup.html`, `login.html`, `setup/*.html` in
`../system/docs-portal/public/`) hardcode this URL in their nav bar brand link
and in "Home / Services / Docs / Contact" menu entries. Forks wanting to
white-label should `sed`-replace `https://OrcheStack.africa` across
those files.

## Regenerating docs

The `docs/` subfolder is generated from `_generate_docs.py`:

```sh
python3 _generate_docs.py
```

This writes 28 HTML pages under `public/docs/`. All navigation, sidebar, and
inter-page links are derived from the single `SIDEBAR` structure at the top of
the generator, so adding a new doc page is a one-line edit in that list.

## Hosting

OrcheStack hosts the site through a static-hosting provider fronted by
Cloudflare. The site is pure static HTML/CSS/SVG so any modern static host
works:

- **Cloudflare Pages** — current default. Points at `public/` on the `master`
  branch; Cloudflare handles TLS, CDN, analytics, and the custom domain.
- **Vercel** / **Netlify** / **GitHub Pages** — drop-in alternatives if the
  hosting choice ever changes.

No Dockerfile or bespoke nginx config is shipped with this folder by design:
the site has no server-side logic, so the hosting provider's static pipeline
is all that's needed.

### If you move off Cloudflare

A minimal Cloudflare Pages setup is:

- Build command: `python3 _generate_docs.py`
- Build output directory: `public/`

Equivalent Vercel / Netlify settings are straightforward — point the build
output at `public/` and run the same generator in the build step if you want
CI to regenerate docs on each commit. Otherwise, pre-generate locally and
commit the output.
