"""
OrcheStack documentation site generator.

Single source of truth for:
  - Sidebar structure (groups + pages in each)
  - Page metadata (title, lede) and body content
  - On-this-page TOC anchors

Regenerates every file under public/docs/ with a consistent shell.
Path depth is handled automatically: pages directly under /docs/ get
relative paths like `../assets/...`; pages under /docs/services/ get
`../../assets/...`.

Run:
    python3 _generate_docs.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).parent
# Output structure changed 2026-05-31:
#   - Marketing pages (index.html, services.html, contact.html) live at ROOT.
#   - Docs site lives at ROOT/docs/.
#   - The earlier `public/` wrapper is gone; Cloudflare Pages now publishes from ROOT.
DOCS = ROOT / "docs"


# =============================================================================
# 1. Canonical sidebar structure
# =============================================================================
# Each group is (title, [(href_from_docs_root, label), ...]).
# `href_from_docs_root` is always relative to DOCS, e.g. "install.html" or
# "services/dbt.html". The generator computes the correct prefix per page.

SIDEBAR: list[tuple[str, list[tuple[str, str]]]] = [
    ("Get started", [
        ("index.html",         "Overview"),
        ("install.html",       "Step 1 — Install OrcheStack"),
        ("signup.html",        "Step 2 — Sign up"),
        ("configure.html",     "Step 3 — Configure services"),
        ("first-pipeline.html","Step 4 — Run first pipeline"),
    ]),
    ("Concepts", [
        ("architecture.html",       "Architecture"),
        ("hot-cold-tiers.html",     "Hot & cold tiers"),
        ("roles-permissions.html",  "Roles & permissions"),
        ("sessions.html",           "Service sessions"),
        ("account-management.html", "Account management"),
    ]),
    ("Guides", [
        ("guides/dbt-to-github.html",   "Pushing your dbt project to GitHub"),
        ("guides/dbt-dev-schemas.html", "Multi-team dev schemas with dbt"),
    ]),
    ("Services", [
        ("services/airbyte.html",             "Airbyte"),
        ("services/airflow.html",             "Apache Airflow"),
        ("services/dbt.html",                 "dbt Core"),
        ("services/postgres.html",            "PostgreSQL"),
        ("services/minio.html",               "MinIO"),
        ("services/metabase.html",            "Metabase"),
        ("services/openmetadata.html",        "OpenMetadata"),
        ("services/great-expectations.html",  "Great Expectations"),
        ("services/pgadmin.html",             "pgAdmin"),
    ]),
    ("Operations", [
        ("credentials.html",     "Managing credentials"),
        ("backup-restore.html",  "Backup & restore"),
        ("upgrading.html",       "Upgrading OrcheStack"),
        ("troubleshooting.html", "Troubleshooting"),
    ]),
    ("Reference", [
        ("cli.html",                 "CLI commands"),
        ("api.html",                 "REST API"),
        ("compose-reference.html",   "docker-compose reference"),
    ]),
]


# =============================================================================
# 2. Page data
# =============================================================================
@dataclass
class Page:
    """Definition of a single docs page."""

    path: str                         # relative to DOCS, e.g. "install.html"
    title: str                        # browser tab title (without " — OrcheStack")
    h1: str                           # main heading
    lede: str                         # one-sentence intro under h1
    body: str                         # HTML body content (inside <main>, after lede)
    toc: list[tuple[str, str]]        # [(anchor_id, label), ...] for "On this page"
    breadcrumb: list[tuple[str, str]] = field(default_factory=list)
    # breadcrumb entries: (href_or_None, label). href=None renders as plain text (last crumb).


# ---- Get Started group ------------------------------------------------------
PAGES: list[Page] = [
    Page(
        path="index.html",
        title="Documentation",
        h1="Get started with OrcheStack",
        lede="OrcheStack is a containerised, cost-effective open-source data platform for Nigerian organisations. Deploy a complete modern data stack on a single host — pick your tools, configure credentials, and OrcheStack pulls and starts only what you enabled.",
        breadcrumb=[(None, "Overview")],
        toc=[
            ("what-is-OrcheStack", "What is OrcheStack?"),
            ("how-install-works", "How the install works"),
            ("who-is-OrcheStack-for", "Who is OrcheStack for?"),
            ("next-steps", "Next steps"),
        ],
        body="""\
<h2 id="what-is-OrcheStack">What is OrcheStack?</h2>
<p>OrcheStack bundles the modern data stack — ingestion, transformation, storage, quality, governance, and BI — behind a unified <strong>dashboard</strong>. Instead of learning Kubernetes, wiring ten tools together, or paying for cloud-managed services, you run <code class="inline">docker compose up OrcheStack</code> on any host and configure the platform through a browser dashboard.</p>
<p>OrcheStack is built for teams that want the full flexibility of open-source tools without the operational burden of running them separately.</p>

<h2 id="how-install-works">How the install works</h2>
<p>Unlike traditional data-stack deployments, OrcheStack <strong>does not pull every service upfront</strong>. The base install brings up only the control plane:</p>
<ul>
  <li>Reverse Proxy (routing + TLS termination)</li>
  <li>Front-facing site (this page you're reading)</li>
  <li>OrcheStack dashboard (HTMX + FastAPI + Tailwind, admin UI)</li>
  <li>PostgreSQL (for user accounts and later your warehouse)</li>
  <li>Service Orchestrator (manages service lifecycles)</li>
</ul>
<p>After you sign up and pick your tools, OrcheStack pulls their Docker images and writes a generated <code class="inline">docker-compose.yml</code>. Services marked <em>hot</em> (like Metabase) stay running; <em>cold</em> services (like Airbyte or dbt) spin up only when triggered by Airflow or by clicking a button in the dashboard.</p>
<div class="callout">
  <p><strong>Why lazy-pull?</strong> It keeps your base install under 2 GB RAM and ensures you only use disk for tools you actually chose. A Nigerian SME laptop or a $500 VPS is enough to run the whole platform.</p>
</div>

<h2 id="who-is-OrcheStack-for">Who is OrcheStack for?</h2>
<p>OrcheStack is designed for:</p>
<ul>
  <li><strong>SMEs and startups</strong> that want enterprise-grade analytics without enterprise pricing.</li>
  <li><strong>Data teams</strong> moving off spreadsheets toward a proper warehouse + BI.</li>
  <li><strong>Analysts and engineers</strong> who prefer open-source tools they can inspect, fork, and redeploy.</li>
  <li><strong>Organisations in resource-constrained environments</strong> (intermittent internet, limited cloud access) where a single-host deployment beats a cloud-native one.</li>
</ul>

<h2 id="next-steps">Next steps</h2>
<p>Ready to try it? Follow the four-step install walkthrough.</p>
""",
    ),

    Page(
        path="install.html",
        title="Step 1 — Install OrcheStack",
        h1="Step 1 — Install OrcheStack",
        lede="OrcheStack ships as Docker images on Docker Hub plus a compose file on GitHub. Most people install via a single installer script; power users clone the repo instead.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Get started"), (None, "Install OrcheStack")],
        toc=[
            ("prerequisites", "Prerequisites"),
            ("distribution", "Where OrcheStack lives"),
            ("install-script", "Option 1 — Installer script (recommended)"),
            ("install-clone", "Option 2 — Clone the GitHub repo"),
            ("install-manual", "Option 3 — Manual compose"),
            ("verify", "Verify the install"),
        ],
        body="""\
<h2 id="prerequisites">Prerequisites</h2>
<ul>
  <li><strong>Docker</strong> 24+ and <strong>Docker Compose</strong> v2.20+</li>
  <li><strong>Host machine</strong> with at least 4 GB RAM and 20 GB disk free (the base install uses ~2 GB)</li>
  <li><strong>Linux, macOS, or Windows with WSL2</strong>. Any POSIX-style host works.</li>
  <li><strong>Ports 80 and 443</strong> available on the host (OrcheStack's reverse proxy binds here)</li>
</ul>
<div class="callout">
  <p><strong>Working behind a firewall?</strong> You'll need outbound HTTPS to <code class="inline">hub.docker.com</code> (Docker images) and <code class="inline">orchestack.africa</code> (installer + compose file). Everything else is internal.</p>
</div>

<h2 id="distribution">Where OrcheStack lives</h2>
<p>OrcheStack is three artifacts in three places — understanding the split makes the install paths below obvious:</p>
<ul>
  <li><strong>Docker Hub</strong> (<code class="inline">hub.docker.com/r/tripleaceme/orchestack-*</code>) — our prebuilt images: <code class="inline">tripleaceme/orchestack-auth</code> (signup, login and setup wizard), <code class="inline">tripleaceme/orchestack-orchestrator</code> (hot/cold service lifecycle daemon), <code class="inline">tripleaceme/orchestack-dashboard</code> (administrator dashboard), and <code class="inline">tripleaceme/orchestack-airflow</code> (Apache Airflow with dbt + Cosmos preinstalled). These are what <code class="inline">docker compose</code> pulls.</li>
  <li><strong>GitHub</strong> (<code class="inline">github.com/tripleaceme/orchestack-public</code>) — source of truth for the compose file, the service generator, docs, and the setup skeleton. This is what you clone if you want to read, fork, or contribute.</li>
  <li><strong>orchestack.africa</strong> — marketing + docs + the installer script you're about to run. The installer is a thin shell script that pulls from the other two places.</li>
</ul>
<p>Pick an install option below based on how much you want to see under the hood.</p>

<h2 id="install-script">Option 1 — Installer script <span class="muted" style="font-weight:500">(recommended)</span></h2>
<p>Single command. Good for production hosts, demo laptops, and CI runners that just need OrcheStack running.</p>
<pre>curl -fsSL https://orchestack.africa/install | sh</pre>
<p>The installer creates an <code class="inline">orchestack/</code> directory in your current path, downloads the latest pinned <code class="inline">docker-compose.yml</code>, runs <code class="inline">docker compose up -d</code>, and prints the URL to visit when the control plane is up. It prompts before overwriting an existing install.</p>
<div class="callout">
  <p><strong>Pin to a specific version</strong> by passing <code class="inline">ORCHESTACK_VERSION=1.2.0 curl ... | sh</code>. Default is the latest stable tag.</p>
</div>

<h2 id="install-clone">Option 2 — Clone the GitHub repo</h2>
<p>For operators who want to read every file before running it, or who plan to fork and customise.</p>
<pre>git clone https://github.com/tripleaceme/orchestack-public
cd orchestack
docker compose up -d</pre>
<p>Same result as option 1, but the compose file and all supporting scripts live in a git-tracked folder you own. Updates happen via <code class="inline">git pull</code> + <code class="inline">docker compose pull</code>.</p>

<h2 id="install-manual">Option 3 — Manual compose</h2>
<p>If you want to inspect or edit the compose file before starting anything — CI pipelines, Kubernetes migrations, airgapped hosts.</p>
<pre>mkdir orchestack && cd orchestack
curl -fsSL https://raw.githubusercontent.com/tripleaceme/orchestack-public/main/docker-compose.yml -o docker-compose.yml
# inspect / edit as needed
docker compose up -d</pre>
<p>This is what options 1 and 2 do under the hood. Use it when you need to change the compose file (e.g. change exposed ports, add a volume mount) before the first boot.</p>

<h2 id="verify">Verify the install</h2>
<p>All three options end at the same place. You should see:</p>
<pre>[+] Running 4/4
 ✔ Container orchestack-proxy        Started
 ✔ Container orchestack-auth         Started
 ✔ Container orchestack-postgres     Started
 ✔ Container orchestack-dashboard     Started</pre>
<p>Only four containers at this stage. No Airbyte, dbt, Metabase, or any other service is pulled yet — those come after you configure the platform.</p>
<p>Open a browser and go to <code class="inline">http://localhost</code>. You should land on the OrcheStack signup page (because no users exist yet).</p>
<div class="callout warn">
  <p><strong>Port conflict?</strong> If something else is using port 80 on the host, edit the <code class="inline">PROXY_HTTP_PORT</code> variable in your <code class="inline">.env</code> file and restart.</p>
</div>
""",
    ),

    Page(
        path="signup.html",
        title="Step 2 — Sign up",
        h1="Step 2 — Sign up",
        lede="Create your first OrcheStack account. The first person to sign up becomes Admin automatically — no bootstrap shell scripts, no command-line user creation.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Get started"), (None, "Sign up")],
        toc=[
            ("first-user-is-admin", "First user is Admin"),
            ("the-signup-form", "The signup form"),
            ("what-happens-next", "What happens next"),
            ("adding-more-users", "Adding more users"),
        ],
        body="""\
<h2 id="first-user-is-admin">First user is Admin</h2>
<p>When OrcheStack starts for the first time, the <code class="inline">platform.users</code> table is empty. The signup page detects this and automatically promotes whoever registers first to <strong>Admin</strong>. This mirrors how Metabase, Airflow, Grafana, and every self-hosted admin tool handles initial installation.</p>
<p>This means you don't need to run any <code class="inline">CREATE USER</code> SQL before using OrcheStack. The web UI bootstraps itself.</p>

<h2 id="the-signup-form">The signup form</h2>
<p>Open the browser and navigate to <code class="inline">http://localhost/signup</code> (or click <strong>Get started</strong> on the landing page). Provide:</p>
<ul>
  <li><strong>Full name</strong> — appears in the admin dashboard and audit logs.</li>
  <li><strong>Email</strong> — used for password resets and notifications. Must be unique.</li>
  <li><strong>Username</strong> — 3–32 characters; letters, numbers, dot, dash, underscore. Used for login.</li>
  <li><strong>Role</strong> — locked to <em>Admin</em> for the first user. Future users pick Admin, Engineer, Analyst, or a custom role.</li>
  <li><strong>Password</strong> — minimum 12 characters. Stored bcrypt-hashed in PostgreSQL.</li>
  <li><strong>Company name</strong> — shown on your dashboard and exported report headers. Metadata only.</li>
</ul>

<h2 id="what-happens-next">What happens next</h2>
<p>On submit, OrcheStack:</p>
<ol>
  <li>Inserts a row in <code class="inline">platform.users</code> with your details. The password is bcrypt-hashed (cost factor 12) before storage.</li>
  <li>Creates a session in <code class="inline">platform.sessions</code> and issues you a secure, HTTP-only session cookie (12-hour TTL by default).</li>
  <li>Redirects you to the OrcheStack admin dashboard at <code class="inline">/app</code>.</li>
  <li>You land on the <strong>service selection</strong> page — see <a href="configure.html">Step 3 — Configure services</a>.</li>
</ol>
<div class="callout">
  <p><strong>Want to change your password or email later?</strong> See <a href="account-management.html">Account management</a> — you manage your own credentials from the admin dashboard without touching the database.</p>
</div>

<h2 id="adding-more-users">Adding more users</h2>
<p>After the first user bootstrap, <strong>self-signup is disabled</strong>. New users are added by an Admin from the dashboard <strong>Users</strong> page. This prevents strangers from registering if your OrcheStack instance is exposed publicly.</p>
<p>See <a href="roles-permissions.html">Roles &amp; permissions</a> for how to design the permission matrix for each role.</p>
""",
    ),

    Page(
        path="configure.html",
        title="Step 3 — Configure services",
        h1="Step 3 — Configure services",
        lede="Pick one tool per pipeline layer, provide credentials, and OrcheStack pulls the Docker images you chose — writing a generated <code class=\"inline\">docker-compose.yml</code> and <code class=\"inline\">.env</code> under the hood.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Get started"), (None, "Configure services")],
        toc=[
            ("pick-tools", "Pick your tools"),
            ("enter-credentials", "Enter credentials"),
            ("pull-and-start", "Pull and start"),
            ("editing-later", "Editing credentials later"),
        ],
        body="""\
<h2 id="pick-tools">Pick your tools</h2>
<p>After signing up, you land on the <strong>Service selection</strong> page. Each pipeline layer has a dropdown with a tested default and documented alternatives:</p>
<ul>
  <li><strong>Ingestion</strong> — Airbyte <em>(recommended)</em>, Custom Python, None</li>
  <li><strong>Orchestration</strong> — Apache Airflow (core, always-on)</li>
  <li><strong>Warehouse</strong> — PostgreSQL <em>(recommended)</em>, ClickHouse, DuckDB</li>
  <li><strong>Data lake</strong> — MinIO (core)</li>
  <li><strong>Transformation</strong> — dbt Core <em>(recommended)</em>, SQLMesh</li>
  <li><strong>Data quality</strong> — Great Expectations <em>(recommended)</em>, Soda Core, None</li>
  <li><strong>Governance</strong> — OpenMetadata <em>(recommended)</em>, DataHub, None</li>
  <li><strong>BI</strong> — Metabase <em>(recommended)</em>, Apache Superset, Lightdash</li>
  <li><strong>Database admin UI</strong> — pgAdmin <em>(recommended)</em>, Adminer, pgweb, None</li>
</ul>
<p>Pick the defaults if you're unsure — they're the ones OrcheStack tests end-to-end.</p>

<h2 id="enter-credentials">Enter credentials</h2>
<p>For each service you enabled, OrcheStack shows a credentials form. The fields are specific to that tool — for example, OpenMetadata asks for:</p>
<ul>
  <li>Admin email and password</li>
  <li>JWT secret (32+ characters)</li>
  <li>PostgreSQL backend details (host, port, database, user, password)</li>
</ul>
<p>OrcheStack runs a live connection test against every backend before allowing you to proceed. Invalid credentials fail fast — no container rebuilds burning time.</p>
<div class="callout">
  <p><strong>Where credentials go.</strong> OrcheStack writes them to a file at <code class="inline">./config/.env</code> on the host, <code class="inline">chmod 600</code>-protected. Your <code class="inline">docker-compose.yml</code> references them via <code class="inline">${VAR}</code> placeholders. <strong>Add <code class="inline">./config</code> to your <code class="inline">.gitignore</code></strong> before committing anything — you don't want credentials in source control.</p>
</div>

<h2 id="pull-and-start">Pull and start</h2>
<p>Once you click <strong>Save &amp; deploy</strong>, OrcheStack does three things:</p>
<ol>
  <li>Regenerates <code class="inline">docker-compose.yml</code> with just the services you picked.</li>
  <li>Pulls each service's Docker image (first time only — cached after).</li>
  <li>Runs <code class="inline">docker compose up -d</code> to start hot-tier services and register cold-tier services with the orchestrator.</li>
</ol>
<p>Expect the first-time pull to take 2–5 minutes depending on your internet connection. Subsequent saves are near-instant.</p>

<h2 id="editing-later">Editing credentials later</h2>
<p>From the OrcheStack admin dashboard, click any service tile and choose <strong>Edit config</strong>. Saving a change writes the new value to <code class="inline">.env</code> and issues:</p>
<pre>docker compose up -d --force-recreate &lt;service&gt;</pre>
<p>Container recreation picks up the new environment variables. Data volumes are preserved, so you don't lose history when rotating a password.</p>
<div class="callout warn">
  <p><strong>Heads up for admins.</strong> Any credential change triggers a container recreation. If a service currently has active user sessions (e.g. someone is using pgAdmin), OrcheStack will ask you to confirm because recreation disconnects them. Scheduled updates during off-hours are safest.</p>
</div>
""",
    ),

    Page(
        path="first-pipeline.html",
        title="Step 4 — Run first pipeline",
        h1="Step 4 — Run your first pipeline",
        lede="With OrcheStack configured, trigger your first end-to-end data pipeline — ingest, transform, test, serve — and verify the results land in Metabase.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Get started"), (None, "Run first pipeline")],
        toc=[
            ("demo-dataset", "Load the demo dataset"),
            ("trigger-run", "Trigger the pipeline"),
            ("monitor", "Monitor the run"),
            ("verify", "Verify the output"),
            ("schedule", "Schedule nightly runs"),
        ],
        body="""\
<h2 id="demo-dataset">Load the demo dataset</h2>
<p>OrcheStack ships a demo dataset — a smallholder agricultural cooperative with yield records, market prices, and supply-chain events — to let you run the pipeline end-to-end without connecting a real source first.</p>
<p>From the OrcheStack admin dashboard, click <strong>Load demo data</strong>. OrcheStack does three things:</p>
<ol>
  <li>Drops the demo CSVs into MinIO under <code class="inline">s3://OrcheStack/raw/demo/</code>.</li>
  <li>Configures an Airbyte connector that watches that bucket for new files.</li>
  <li>Creates a pre-built dbt project under <code class="inline">./dbt/</code> with staging and marts models for the demo schema.</li>
</ol>

<h2 id="trigger-run">Trigger the pipeline</h2>
<p>Click <strong>▶ Run pipeline now</strong> on the admin dashboard. The Orchestrator:</p>
<ol>
  <li>Spins up Airbyte (cold tier) and syncs the demo CSVs into PostgreSQL <code class="inline">raw</code> schema.</li>
  <li>Spins up dbt (cold tier) and runs <code class="inline">dbt build --select tag:demo</code>, which builds <code class="inline">stg_yields</code>, <code class="inline">stg_prices</code>, and <code class="inline">fct_coop_performance</code> in the <code class="inline">marts</code> schema.</li>
  <li>Runs Great Expectations on the mart — checks that yields are positive, prices are not null, and the state column matches the 36 Nigerian states plus FCT.</li>
  <li>Runs Elementary to record test results.</li>
  <li>Shuts down the cold services.</li>
</ol>

<h2 id="monitor">Monitor the run</h2>
<p>While the pipeline runs, the dashboard shows live progress. Each stage streams logs into the dashboard. You can also watch individual task states in the Airflow UI — click <strong>Open Airflow</strong> in the dashboard.</p>
<p>A full run of the demo pipeline typically takes 45–90 seconds on a 4-core laptop. Your first run will be slower (~3 minutes) because Airbyte and dbt container images are being pulled.</p>

<h2 id="verify">Verify the output</h2>
<p>When the run completes, click <strong>Open Metabase</strong>. The demo dashboard shows:</p>
<ul>
  <li>Total yield by state (bar chart)</li>
  <li>Average market price by crop (trend line)</li>
  <li>Cooperative performance scorecard</li>
</ul>
<p>If you see numbers, your pipeline is live. You've proven the end-to-end path: raw CSV → MinIO → PostgreSQL raw → dbt marts → Metabase dashboard.</p>

<h2 id="schedule">Schedule nightly runs</h2>
<p>By default, OrcheStack creates an Airflow DAG called <code class="inline">OrcheStack_nightly</code> that runs at 02:00 local time. You can edit its schedule from the dashboard's <strong>Pipelines</strong> page or directly in the Airflow UI. The DAG re-runs the full pipeline and emails a summary if tests fail.</p>
<div class="callout">
  <p><strong>Using your own data?</strong> Replace the demo connector with an Airbyte connector for your source (Postgres, Google Sheets, Stripe, etc.), update the dbt models under <code class="inline">./dbt/models/</code>, and run the same pipeline. Everything else stays the same.</p>
</div>
""",
    ),

    # ---- Concepts group ----
    Page(
        path="architecture.html",
        title="Architecture",
        h1="Architecture",
        lede="OrcheStack is a three-state system: a minimal base that ships in every install, a hot tier that appears after configuration, and a cold tier that spins up on demand during pipeline runs.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Concepts"), (None, "Architecture")],
        toc=[
            ("three-states", "Three states"),
            ("why-three-states", "Why three states matter"),
            ("reverse-proxy", "Reverse proxy"),
            ("storage-layout", "Storage layout"),
        ],
        body="""\
<h2 id="three-states">Three states</h2>
<p>Traditional data-stack deployments pull every tool upfront — a "worst-case RAM" footprint that wastes resources when only parts of the stack are in use. OrcheStack instead pulls and starts services according to what's actually needed right now.</p>

<h3 id="state-base">1. Base install (~2 GB)</h3>
<p>Runs from the first <code class="inline">docker compose up OrcheStack</code>. Contains only the control plane:</p>
<ul>
  <li><strong>Reverse Proxy</strong> — routes HTTP traffic, terminates TLS, serves the landing page</li>
  <li><strong>Front-facing site</strong> — this docs site, plus home, services, and contact pages</li>
  <li><strong>Dashboard</strong> — admin UI, HTMX + FastAPI + Tailwind (served after auth)</li>
  <li><strong>PostgreSQL</strong> — stores user accounts, roles, permissions, session ledger</li>
  <li><strong>Service Orchestrator</strong> — Python process managing hot/cold lifecycles</li>
</ul>

<h3 id="state-configured">2. Configured (~4–5 GB)</h3>
<p>After the admin picks tools and enters credentials, OrcheStack pulls the selected services and brings up those marked <em>hot</em> to stay running:</p>
<ul>
  <li><strong>MinIO</strong> — data lake for raw files</li>
  <li><strong>Metabase</strong> (or Superset / Lightdash) — BI for stakeholders</li>
  <li><strong>Airflow scheduler</strong> — the clock that triggers cold services on schedule</li>
</ul>

<h3 id="state-active">3. Active pipeline (~7–10 GB peak)</h3>
<p>During a scheduled DAG run or when an engineer manually triggers work, OrcheStack spins up cold-tier services for the duration of the task:</p>
<ul>
  <li><strong>Airbyte</strong> — ingestion window</li>
  <li><strong>Airflow workers</strong> — task execution</li>
  <li><strong>dbt Core</strong> — SQL transformations</li>
  <li><strong>Great Expectations</strong> — data quality checks</li>
  <li><strong>Elementary</strong> — dbt observability</li>
  <li><strong>pgAdmin</strong>, <strong>OpenMetadata</strong> — when an engineer opens them</li>
</ul>
<p>Cold services stop when their task completes and the reference-counted session count reaches zero.</p>

<h2 id="why-three-states">Why three states matter</h2>
<p>A naive deployment would keep every service running constantly. For a 10-tool stack, that's ~15 GB of RAM sitting idle between pipeline runs. OrcheStack's three-state model means:</p>
<ul>
  <li>Your baseline resource cost is ~2 GB — what the control plane needs</li>
  <li>Steady-state cost is ~4–5 GB — what stakeholders need to query BI</li>
  <li>Peak cost is bounded to pipeline runtime — typically minutes per day</li>
</ul>
<div class="callout">
  <p><strong>Total Cost of Ownership impact.</strong> For a Nigerian SME running OrcheStack on a $40/month VPS, the three-state model is what makes the platform viable. A 16 GB VPS would cost 3–4× more than the 8 GB one OrcheStack actually needs at peak.</p>
</div>

<h2 id="reverse-proxy">Reverse proxy as unified entry</h2>
<p>Every service with a native web UI is reachable through the same domain via the reverse proxy:</p>
<pre>/                → Front-facing site (landing, services, contact)
/docs/*          → This docs site
/app             → Dashboard (after auth)
/app/metabase    → Metabase (hot)
/app/airflow     → Airflow UI (hot)
/app/airbyte     → Airbyte (cold — spun up on click)
/app/openmeta    → OpenMetadata (cold)
/app/pgadmin     → pgAdmin (cold)</pre>
<p>Clicking any cold-service URL in the dashboard triggers the orchestrator to spin it up, then redirects you to the proxy route once a health check passes.</p>

<h2 id="storage-layout">Storage layout</h2>
<p>OrcheStack uses a single PostgreSQL instance with multiple schemas to avoid database sprawl:</p>
<ul>
  <li><code class="inline">platform</code> — user accounts, roles, permissions, sessions, audit log</li>
  <li><code class="inline">raw</code> — landed data from Airbyte, waiting for dbt</li>
  <li><code class="inline">marts</code> — dbt outputs consumed by Metabase</li>
  <li><code class="inline">airflow</code> and <code class="inline">openmetadata</code> schemas for those services' metadata</li>
</ul>
<p>MinIO holds raw files as an alternate substrate for AI / ML / DS consumers who prefer objects over rows.</p>
""",
    ),

    Page(
        path="hot-cold-tiers.html",
        title="Hot & cold tiers",
        h1="Hot & cold tiers",
        lede="The hot/cold split is how OrcheStack fits a full data stack inside a single SME-scale host without running services that aren't being used.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Concepts"), (None, "Hot & cold tiers")],
        toc=[
            ("definition", "Definition"),
            ("assignment-rules", "Assignment rules"),
            ("lifecycle", "Cold-service lifecycle"),
            ("resource-comparison", "Resource comparison"),
        ],
        body="""\
<h2 id="definition">Definition</h2>
<p>Every OrcheStack-managed service falls into one of three classes:</p>
<ul>
  <li><strong>Base</strong> — ships with the control plane. Always running. Cannot be disabled.</li>
  <li><strong>Hot</strong> — pulled on configuration, stays running while enabled. Used by services that stakeholders or schedulers need to reach at any time.</li>
  <li><strong>Cold</strong> — pulled on configuration, runs only when a task or an engineer's click demands it. Stops when idle.</li>
</ul>

<h2 id="assignment-rules">Assignment rules</h2>
<p>The principle is simple: <strong>services that need to respond to external traffic at any moment are hot; services that respond to triggered work are cold</strong>.</p>
<ul>
  <li><strong>Metabase</strong> — hot. A stakeholder might open a dashboard at any time.</li>
  <li><strong>Airflow scheduler</strong> — hot. It is the clock; without it, nothing fires.</li>
  <li><strong>MinIO</strong> — hot. The data lake must always be readable by downstream consumers.</li>
  <li><strong>Airbyte</strong> — cold. Only runs during the ingestion window.</li>
  <li><strong>dbt</strong> — cold. A CLI, not a service. Runs when Airflow triggers it.</li>
  <li><strong>Great Expectations, Elementary</strong> — cold. Run after dbt finishes.</li>
  <li><strong>pgAdmin</strong> — cold. Ad-hoc engineer tool, not stakeholder-facing.</li>
  <li><strong>OpenMetadata</strong> — cold. Heavy (~3 GB); engineers open it to explore lineage, then close it.</li>
</ul>

<h2 id="lifecycle">Cold-service lifecycle</h2>
<p>Cold services transition through four states: <code class="inline">STOPPED</code> → <code class="inline">STARTING</code> → <code class="inline">RUNNING</code> → <code class="inline">STOPPING</code>. Transitions are driven by three triggers:</p>
<ol>
  <li><strong>Airflow DAG schedule</strong> — e.g. the nightly ETL at 02:00 starts Airbyte + workers + dbt + Elementary in sequence.</li>
  <li><strong>Dashboard manual button</strong> — e.g. "Run dbt now" or "Open pgAdmin".</li>
  <li><strong>Upstream dependency</strong> — e.g. when dbt finishes, Elementary auto-starts to record test results.</li>
</ol>
<p>A cold service only stops when its <em>reference-counted session count</em> hits zero AND the idle timeout has elapsed. See <a href="sessions.html">Service sessions</a> for the multi-user safety this provides.</p>

<h2 id="resource-comparison">Resource comparison</h2>
<table>
  <thead><tr><th>State</th><th>Services running</th><th>Est. RAM</th></tr></thead>
  <tbody>
    <tr><td>Base install</td><td>Proxy, Front, Dashboard, PostgreSQL, Orchestrator</td><td>~2 GB</td></tr>
    <tr><td>Configured (idle)</td><td>Base + hot tier (MinIO, Metabase, Airflow scheduler)</td><td>~4–5 GB</td></tr>
    <tr><td>Active pipeline</td><td>Configured + cold tier during a run</td><td>~7–10 GB peak</td></tr>
  </tbody>
</table>
""",
    ),

    Page(
        path="roles-permissions.html",
        title="Roles & permissions",
        h1="Roles & permissions",
        lede="OrcheStack ships with three system roles — Admin, Engineer, Analyst — and lets Admins create additional roles with fine-grained per-service permissions.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Concepts"), (None, "Roles & permissions")],
        toc=[
            ("role-model", "The role model"),
            ("permission-matrix", "Permission matrix"),
            ("system-roles", "System roles"),
            ("custom-roles", "Creating custom roles"),
            ("enforcement", "How enforcement works"),
        ],
        body="""\
<h2 id="role-model">The role model</h2>
<p>Every user has exactly one <strong>role</strong>. A role is a named bundle of permissions. Permissions are defined <em>per service</em> — the same role can have different capabilities on different services.</p>

<h2 id="permission-matrix">Permission matrix</h2>
<p>For each (role, service) pair, four permissions apply:</p>
<ul>
  <li><code class="inline">can_start</code> — may initiate service spin-up (cold tier only)</li>
  <li><code class="inline">can_use</code> — may access the service's UI while it's running</li>
  <li><code class="inline">can_force_stop</code> — may kill the container even when other users have active sessions (emergency)</li>
  <li><code class="inline">can_edit_config</code> — may modify the service's credentials / env vars</li>
</ul>

<h2 id="system-roles">System roles</h2>
<table>
  <thead><tr><th>Role</th><th>Can do</th><th>Typical user</th></tr></thead>
  <tbody>
    <tr><td><strong>Admin</strong></td><td>Everything on every service. Adds/removes users. Configures roles.</td><td>Platform owner, CTO</td></tr>
    <tr><td><strong>Engineer</strong></td><td>Start / use / edit configs on all services. Cannot force-stop. Cannot manage users.</td><td>Analytics engineer, data engineer</td></tr>
    <tr><td><strong>Analyst</strong></td><td>Use Metabase + OpenMetadata + pgAdmin only. Cannot start infra. Cannot force-stop.</td><td>Business analyst, data analyst</td></tr>
  </tbody>
</table>

<h2 id="custom-roles">Creating custom roles</h2>
<p>From the OrcheStack admin dashboard, go to <strong>Users → Roles → New role</strong>. For each service, tick the permissions you want. Save.</p>
<p>Example — an "Analytics Engineer" role that can operate dbt and pgAdmin but has read-only access to Metabase:</p>
<pre>service     | can_start | can_use | can_force_stop | can_edit_config
dbt         | ✓         | ✓       |                | ✓
pgadmin     | ✓         | ✓       |                |
postgres    |           | ✓       |                |
metabase    |           | ✓       |                |
airbyte     | ✓         | ✓       |                | ✓</pre>

<h2 id="enforcement">How enforcement works</h2>
<p>Every protected dashboard page has a guard at the top that checks the current user's role permission against the service they are trying to interact with. Unauthorised attempts redirect to the dashboard with a friendly error message.</p>
<p>Below the application layer, PostgreSQL role-level privileges provide defence-in-depth: an Analyst role in PostgreSQL lacks <code class="inline">CREATE</code> and <code class="inline">DROP</code> privileges on the <code class="inline">marts</code> schema, so even a compromised dashboard process cannot elevate their access.</p>
<div class="callout">
  <p><strong>Audit trail.</strong> Every permission-sensitive action (role creation, force-stop, credential edit) writes a row to <code class="inline">platform.audit_log</code> with actor, timestamp, and target. Admins can review these from the <strong>Audit</strong> tab.</p>
</div>
""",
    ),

    Page(
        path="sessions.html",
        title="Service sessions",
        h1="Service sessions",
        lede="Users close their own sessions. The orchestrator owns the container lifecycle. Reference counting keeps one user from disrupting another.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Concepts"), (None, "Service sessions")],
        toc=[
            ("the-problem", "The shared-service problem"),
            ("the-design", "The design"),
            ("heartbeats", "Heartbeats and stale sessions"),
            ("force-stop", "Admin force-stop"),
        ],
        body="""\
<h2 id="the-problem">The shared-service problem</h2>
<p>Two engineers are using pgAdmin at the same time. One finishes their work and clicks "Stop". Under a naive design, pgAdmin's container stops — kicking the other engineer out mid-query. This is the classic shared-resource race condition.</p>
<p>OrcheStack prevents this by separating <strong>user sessions</strong> from <strong>container lifecycle</strong>.</p>

<h2 id="the-design">The design</h2>
<p>Users do not stop services. They <strong>open and close their own sessions</strong>. Each session is a row in <code class="inline">platform.service_sessions</code>.</p>
<p>When a user clicks <strong>Open pgAdmin</strong>:</p>
<ol>
  <li>OrcheStack checks their role's <code class="inline">can_use</code> + <code class="inline">can_start</code>.</li>
  <li>A session row is inserted with status=<code class="inline">active</code>.</li>
  <li>If the container isn't running, the Orchestrator spins it up.</li>
  <li>The user is redirected to <code class="inline">/app/pgadmin</code>.</li>
</ol>
<p>When the user clicks <strong>End my session</strong>:</p>
<ol>
  <li>Their session row is updated to status=<code class="inline">closed</code>.</li>
  <li>The orchestrator checks: are there any other <code class="inline">active</code> sessions for this service?</li>
  <li>If yes, the container keeps running.</li>
  <li>If no, an idle timer starts. After N minutes without a new session, the container is stopped.</li>
</ol>

<h2 id="heartbeats">Heartbeats and stale sessions</h2>
<p>What if someone closes their laptop mid-session without clicking "End my session"? Their row would stay <code class="inline">active</code> forever, keeping the container alive.</p>
<p>Fix: <strong>heartbeats</strong>. The dashboard pings the session endpoint every 30 seconds while the user is on the page. A background job marks any session without a heartbeat for 5 minutes as <code class="inline">stale</code>. Stale sessions don't count.</p>

<h2 id="force-stop">Admin force-stop</h2>
<p>Admins with <code class="inline">can_force_stop=true</code> can kill a container immediately, disconnecting all active sessions. This is intended for stuck or unresponsive services, not routine shutdown.</p>
<p>Every force-stop writes a row to <code class="inline">platform.audit_log</code> with the list of affected users. Admins answer for their overrides.</p>
<div class="callout warn">
  <p>The UI guards the action with a confirmation dialog listing all users currently connected:</p>
  <pre>⚠  Force stop will disconnect 2 users from pgAdmin:
   •  engineer@acme.ng       — active for 14 min
   •  jane@acme.ng           — active for 3 min

   Use only for stuck or unresponsive services.
   All force-stop events are logged to the audit trail.</pre>
</div>
""",
    ),

    Page(
        path="account-management.html",
        title="Account management",
        h1="Managing your account",
        lede="Change your password, email, or username from the OrcheStack dashboard — no SQL, no command-line tools. Admins have extra controls for managing other users.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Concepts"), (None, "Account management")],
        toc=[
            ("where-creds-live", "Where your credentials live"),
            ("change-your-own", "Change your own password, email, or username"),
            ("forgot-password", "Forgot password"),
            ("admin-actions", "What Admins can do"),
            ("deleting-account", "Deleting an account"),
        ],
        body="""\
<h2 id="where-creds-live">Where your credentials live</h2>
<p>Every OrcheStack user account is a row in the <code class="inline">platform.users</code> table inside the base-install PostgreSQL. The schema looks roughly like:</p>
<pre>platform.users
┌──────────────────┬───────────────────────────────────────────┐
│ id               │ serial primary key                         │
│ full_name        │ text                                       │
│ email            │ text unique                                │
│ username         │ text unique                                │
│ password_hash    │ text  -- bcrypt, cost factor 12            │
│ role_id          │ int references platform.roles(id)          │
│ company_name     │ text                                       │
│ created_at       │ timestamptz                                │
│ last_login_at    │ timestamptz                                │
│ is_active        │ boolean default true                       │
└──────────────────┴───────────────────────────────────────────┘</pre>
<p>Passwords are <strong>never stored in plain text</strong>. OrcheStack hashes them with bcrypt before writing to the database; the original is not recoverable. If you forget your password, it must be reset — not retrieved.</p>
<div class="callout">
  <p><strong>You don't need to touch this table directly.</strong> Every field you'd want to change has a UI for it. Going into pgAdmin and running <code class="inline">UPDATE platform.users SET password_hash = ...</code> is unsupported and likely to break things (wrong hash format, stale session cookies, no audit log entry).</p>
</div>

<h2 id="change-your-own">Change your own password, email, or username</h2>
<p>From any page in the OrcheStack dashboard, open the user menu in the top-right and click <strong>Account settings</strong>. The page has three sections:</p>

<h3 id="change-password">Change password</h3>
<p>Enter your current password (for safety) and a new password (minimum 12 characters). OrcheStack:</p>
<ol>
  <li>Verifies the current password by comparing its bcrypt hash to <code class="inline">password_hash</code>.</li>
  <li>Computes a new bcrypt hash for the new password.</li>
  <li>Updates <code class="inline">password_hash</code> in a single transaction.</li>
  <li>Invalidates all your other active sessions (you're logged out everywhere except this tab).</li>
  <li>Writes an entry to <code class="inline">platform.audit_log</code>: <em>"user.password_changed"</em>.</li>
</ol>

<h3 id="change-email">Change email</h3>
<p>Enter your new email and confirm your password. The change is immediate — your next login can use either the old or new email for one hour (grace window), then only the new one.</p>

<h3 id="change-username">Change username</h3>
<p>Enter the new username. OrcheStack checks uniqueness against the <code class="inline">platform.users</code> table. On success, the change is immediate.</p>

<h2 id="forgot-password">Forgot password</h2>
<p>On the login page, click <strong>Forgot?</strong> next to the password field. OrcheStack:</p>
<ol>
  <li>Asks for your email address.</li>
  <li>Generates a one-time reset token, stored in <code class="inline">platform.password_resets</code> with a 30-minute expiry.</li>
  <li>Sends the token to your email (OrcheStack uses the SMTP credentials configured during install; if none, the token is shown to the Admin in the dashboard for manual delivery).</li>
  <li>On click, you land on a form that accepts the new password and consumes the token.</li>
</ol>
<div class="callout warn">
  <p><strong>No email configured?</strong> In a development install where SMTP isn't set up, an Admin can reset any user's password from the <strong>Users</strong> page (see below). The user will be required to change it on next login.</p>
</div>

<h2 id="admin-actions">What Admins can do</h2>
<p>From <strong>Users</strong> in the admin dashboard, an Admin can:</p>
<ul>
  <li><strong>Add a new user</strong> — provide name, email, username, role, and a temporary password. The user is required to change it on first login.</li>
  <li><strong>Reset another user's password</strong> — overrides the bcrypt hash with a new value. Writes an audit entry.</li>
  <li><strong>Change another user's role</strong> — use the dropdown on their row.</li>
  <li><strong>Deactivate a user</strong> — sets <code class="inline">is_active=false</code>. They can no longer log in, but their historical activity and audit trail are preserved.</li>
  <li><strong>Reactivate a user</strong> — the reverse.</li>
  <li><strong>Delete a user</strong> — hard delete. <strong>Prefer deactivate</strong> unless you have a compliance reason to fully remove.</li>
</ul>

<h2 id="deleting-account">Deleting an account</h2>
<p>Regular users cannot delete their own account — only an Admin can. This is intentional to protect against accidental deletion and to ensure the audit trail stays coherent. If you want your account removed, ask your Admin.</p>
""",
    ),

    # ---- Guides group ----
    Page(
        path="guides/dbt-to-github.html",
        title="Pushing your dbt project to GitHub",
        h1="Pushing your dbt project to GitHub",
        lede="OrcheStack pulls your dbt project from a Git repository on every run. Here's how to get your project up on GitHub and connected to OrcheStack, assuming you've never done it before.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Guides"), (None, "Pushing dbt to GitHub")],
        toc=[
            ("prerequisites", "What you need"),
            ("create-repo", "Create the GitHub repository"),
            ("initialise-git", "Initialise Git in your dbt project"),
            ("gitignore", "The critical .gitignore"),
            ("first-push", "First commit and push"),
            ("connect", "Connect the repo to OrcheStack"),
            ("security", "Security — never commit secrets"),
            ("private-repos", "Private repos need a token"),
        ],
        body="""\
<h2 id="prerequisites">What you need</h2>
<ul>
  <li>A GitHub account (GitLab, Bitbucket, or any Git host also work — the steps are the same)</li>
  <li>Git installed locally — check with <code class="inline">git --version</code>. If missing, install from <a href="https://git-scm.com/downloads" target="_blank" rel="noopener">git-scm.com</a>.</li>
  <li>A dbt project folder with a <code class="inline">dbt_project.yml</code> and at least one model under <code class="inline">models/</code></li>
</ul>
<div class="callout">
  <p><strong>New to Git?</strong> The 10-minute official tour — <a href="https://try.github.io/" target="_blank" rel="noopener">try.github.io</a> — covers exactly what you need. You can follow this guide straight after.</p>
</div>

<h2 id="create-repo">Create the GitHub repository</h2>
<ol>
  <li>Open <a href="https://github.com/new" target="_blank" rel="noopener">github.com/new</a>.</li>
  <li>Name the repo something descriptive: <code class="inline">acme-dbt</code>, <code class="inline">analytics-models</code>, <code class="inline">your-company-dbt</code>.</li>
  <li>Choose <strong>Private</strong> unless the whole world should read your SQL. Most teams pick private.</li>
  <li><strong>Don't</strong> tick "Initialize this repository with a README" — we'll push an existing project instead.</li>
  <li>Click <strong>Create repository</strong>. GitHub shows you a URL; keep this tab open.</li>
</ol>

<h2 id="initialise-git">Initialise Git in your dbt project</h2>
<p>Open a terminal and <code class="inline">cd</code> into your dbt project folder:</p>
<pre>cd path/to/your-dbt-project
git init</pre>
<p>If the folder already has a <code class="inline">.git</code> subfolder, skip the <code class="inline">init</code>.</p>

<h2 id="gitignore">The critical .gitignore</h2>
<p>Before you commit anything, tell Git to ignore files that must <strong>never</strong> land in the repo:</p>
<pre># .gitignore
target/
dbt_packages/
logs/
profiles.yml
.user.yml
.env</pre>
<p>Why each one matters:</p>
<ul>
  <li><strong>profiles.yml</strong> — contains your database password. Never commit.</li>
  <li><strong>.env</strong> — contains secrets (API keys, tokens). Never commit.</li>
  <li><strong>target/</strong> — dbt's compiled SQL; regenerated on every run. Noise.</li>
  <li><strong>dbt_packages/</strong> — third-party package source; reinstalled from <code class="inline">packages.yml</code> on every run. Noise.</li>
  <li><strong>logs/</strong> — dbt run logs. Too large and machine-specific to version-control.</li>
</ul>
<p>Save the file as <code class="inline">.gitignore</code> in the dbt project root.</p>

<h2 id="first-push">First commit and push</h2>
<p>Stage everything (respecting .gitignore) and commit:</p>
<pre>git add .
git commit -m "Initial dbt project"</pre>
<p>Connect the GitHub remote (replace with your repo URL from the tab you kept open):</p>
<pre>git remote add origin https://github.com/your-org/your-dbt-repo.git
git branch -M main
git push -u origin main</pre>
<p>Refresh GitHub — your files are now there.</p>

<h2 id="connect">Connect the repo to OrcheStack</h2>
<p>Before pasting the URL into OrcheStack, take ten seconds to verify one thing inside your dbt project: the <code class="inline">profile:</code> value at the top of <code class="inline">dbt_project.yml</code> must match what you entered (or will enter) as the <strong>dbt project name</strong> in OrcheStack. OrcheStack uses that value as the top-level key when it generates <code class="inline">profiles.yml</code>; if they diverge, dbt fails to start with <code class="inline">Could not find profile named 'X'</code>.</p>
<pre># dbt_project.yml — first few lines
name: 'acme_analytics'
version: '1.0.0'
profile: 'acme_analytics'     # ← this is what OrcheStack needs to match</pre>
<p>If the two values already match, you're good. If not, update <code class="inline">profile:</code> in <code class="inline">dbt_project.yml</code> OR update the project name field in OrcheStack — either way, make them identical.</p>
<p>Then, from your OrcheStack OrcheStack dashboard:</p>
<ol>
  <li>Click <strong>Services → dbt Core</strong>.</li>
  <li>Click <strong>Edit config</strong>.</li>
  <li>Confirm <strong>dbt project name</strong> matches your <code class="inline">profile:</code> value.</li>
  <li>Paste the repo URL into <strong>dbt project Git repository</strong>:
    <ul>
      <li>HTTPS: <code class="inline">https://github.com/your-org/your-dbt-repo.git</code></li>
      <li>SSH: <code class="inline">git@github.com:your-org/your-dbt-repo.git</code></li>
    </ul>
  </li>
  <li>Set <strong>Branch to track</strong> to <code class="inline">main</code> (or whichever branch holds production code).</li>
  <li>Click <strong>Save &amp; apply</strong>. OrcheStack will clone the repo into the dbt container.</li>
</ol>
<p>From now on, every scheduled dbt run and every manual "Run dbt now" click pulls the latest commit from the tracked branch before executing.</p>

<h2 id="security">Security — never commit secrets</h2>
<p>Even with a private repo, commits are forever (in Git history). Habits that save you later:</p>
<ul>
  <li><strong>Never put passwords in dbt_project.yml or any SQL file.</strong> Use <code class="inline">env_var()</code> in profiles.yml and keep profiles.yml out of the repo entirely.</li>
  <li><strong>Use .env for local development.</strong> Every developer gets their own .env with their own credentials. The .env file is in .gitignore.</li>
  <li><strong>If you accidentally commit a secret</strong>: rotate it immediately (change the password in PostgreSQL, revoke the token, etc.). The leaked version lives in Git history forever — rotation is the only cure.</li>
</ul>

<h2 id="private-repos">Private repos need a token</h2>
<p>OrcheStack needs permission to clone a private repo. Two approaches:</p>

<h3 id="https-token">HTTPS with a Personal Access Token (easiest)</h3>
<ol>
  <li>On GitHub, go to <strong>Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token</strong>.</li>
  <li>Give the token <strong>Contents: read-only</strong> access to just your dbt repo.</li>
  <li>Copy the token.</li>
  <li>In the OrcheStack dashboard → Services → dbt Core → Edit config, paste the token into <strong>Repo access token</strong>.</li>
</ol>

<h3 id="ssh-deploy-key">SSH with a deploy key (more secure)</h3>
<ol>
  <li>On the OrcheStack host, run <code class="inline">ssh-keygen -t ed25519 -f ./config/dbt/deploy_key -C "OrcheStack-dbt"</code>.</li>
  <li>Copy the public key: <code class="inline">cat ./config/dbt/deploy_key.pub</code>.</li>
  <li>On GitHub, go to the repo's <strong>Settings → Deploy keys → Add deploy key</strong> and paste it. Leave "Allow write access" unchecked — OrcheStack only reads.</li>
  <li>In OrcheStack, switch the repo URL to SSH format (<code class="inline">git@github.com:...</code>) and save.</li>
</ol>
<div class="callout">
  <p><strong>Tip.</strong> Deploy keys scope access to a single repo. Personal access tokens can be scoped to a single repo too (fine-grained tokens) but are easier to leak. Deploy keys are the production-grade choice.</p>
</div>
""",
    ),

    Page(
        path="guides/dbt-dev-schemas.html",
        title="Multi-team dev schemas with dbt Core",
        h1="Multi-team dev schemas with dbt Core",
        lede="Without per-developer schemas, every dbt run overwrites production tables. Here's the pattern most mature analytics teams use, adapted to OrcheStack's single-PostgreSQL setup.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Guides"), (None, "Multi-team dev schemas")],
        toc=[
            ("the-problem", "The problem"),
            ("the-pattern", "The dev-schema pattern"),
            ("admin-setup", "Admin creates the dev schemas"),
            ("developer-profile", "Each developer's profiles.yml"),
            ("local-workflow", "Local development workflow"),
            ("promote-to-prod", "Promoting to production"),
            ("naming", "Naming conventions"),
            ("gotchas", "Gotchas"),
        ],
        body="""\
<h2 id="the-problem">The problem</h2>
<p>Picture this without dev schemas: three engineers all run <code class="inline">dbt run</code> pointing at the production <code class="inline">marts</code> schema. Alice iterates on a new <code class="inline">fct_orders</code> model; Bob starts a migration on the same model; Ayoade builds a dashboard that queries it. At any given moment, nobody knows whether <code class="inline">marts.fct_orders</code> contains Alice's version, Bob's migration, or the last version that actually worked. Metabase dashboards break. Analysts see inconsistent numbers. The team blames each other instead of the toolchain.</p>
<p>The root cause: everyone writing to the same schema. Fix: give each developer their own schema.</p>

<h2 id="the-pattern">The dev-schema pattern</h2>
<p>Each engineer gets a personal schema in the warehouse — <code class="inline">dev_ayoade</code>, <code class="inline">dev_alice</code>, <code class="inline">dev_bob</code>. They run dbt locally with <code class="inline">--target dev</code>, which writes to that personal schema. Production runs happen via OrcheStack's nightly DAG using <code class="inline">--target prod</code>, which writes to the shared <code class="inline">marts</code> schema.</p>
<p>Flow:</p>
<pre>LOCAL  : Alice → dbt run --target dev → writes to dev_alice.fct_orders
LOCAL  : Bob   → dbt run --target dev → writes to dev_bob.fct_orders
PROD   : DAG   → dbt run --target prod → writes to marts.fct_orders  (merged code)</pre>
<p>Metabase, OpenMetadata, and stakeholders only look at <code class="inline">marts</code>. Nothing they see moves unless a PR merges to <code class="inline">main</code> and the nightly DAG runs.</p>

<h2 id="admin-setup">Admin creates the dev schemas</h2>
<p>From pgAdmin (or psql on the host), the platform admin runs this once per engineer:</p>
<pre>-- Create a personal dev schema for each engineer
CREATE SCHEMA dev_ayoade AUTHORIZATION dbt_user;
CREATE SCHEMA dev_alice  AUTHORIZATION dbt_user;
CREATE SCHEMA dev_bob    AUTHORIZATION dbt_user;

-- Grant the dbt_user full control over each schema
GRANT ALL ON SCHEMA dev_ayoade TO dbt_user;
GRANT ALL ON SCHEMA dev_alice  TO dbt_user;
GRANT ALL ON SCHEMA dev_bob    TO dbt_user;

-- Optionally grant read access to analysts for inspection
GRANT USAGE ON SCHEMA dev_ayoade TO analyst_user;
GRANT SELECT ON ALL TABLES IN SCHEMA dev_ayoade TO analyst_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA dev_ayoade
  GRANT SELECT ON TABLES TO analyst_user;</pre>
<div class="callout">
  <p><strong>Smaller team alternative.</strong> If you have 1–2 engineers, a single shared <code class="inline">dev_marts</code> schema is fine. The dbt convention scales up later — you only need per-person schemas when conflicts actually happen.</p>
</div>

<h2 id="developer-profile">Each developer's profiles.yml</h2>
<p>Each engineer puts a <code class="inline">profiles.yml</code> on their laptop at <code class="inline">~/.dbt/profiles.yml</code> (or in the project root — dbt finds both). The file is per-machine; never commit it.</p>
<pre>OrcheStack:
  target: dev                    # default when you just type 'dbt run'
  outputs:
    dev:
      type: postgres
      host: OrcheStack.acme.ng     # or the IP / localhost for self-host
      user: dbt_user
      password: "{{ env_var('DBT_PASSWORD') }}"
      port: 5432
      dbname: OrcheStack
      schema: dev_ayoade         # ← your personal schema
      threads: 4
    prod:
      type: postgres
      host: OrcheStack.acme.ng
      user: dbt_user
      password: "{{ env_var('DBT_PASSWORD') }}"
      port: 5432
      dbname: OrcheStack
      schema: marts              # ← shared production schema
      threads: 8</pre>
<p>Each engineer replaces <code class="inline">dev_ayoade</code> with their own schema name.</p>
<p>Set <code class="inline">DBT_PASSWORD</code> in your shell's <code class="inline">.env</code> or <code class="inline">~/.bashrc</code> — never paste it into profiles.yml directly.</p>

<h2 id="local-workflow">Local development workflow</h2>
<p>Once the profile is configured, the daily loop is:</p>
<pre># Iterate on your model — writes to dev_ayoade.fct_orders
dbt run --select fct_orders

# Test it locally against your dev data
dbt test --select fct_orders

# When you're happy, push to a branch
git checkout -b feature/new-orders-mart
git commit -am "Refactor fct_orders to include returns"
git push origin feature/new-orders-mart</pre>
<p>Notice: you never typed <code class="inline">--target prod</code>. Default is <code class="inline">dev</code>. Production is unreachable by accident.</p>

<h2 id="promote-to-prod">Promoting to production</h2>
<p>Open a pull request on GitHub. The team reviews the model — was the transformation right? Did tests pass?</p>
<p>Once merged to <code class="inline">main</code>, the next OrcheStack nightly DAG run does:</p>
<pre>git pull origin main
dbt build --target prod</pre>
<p>Which rebuilds <code class="inline">marts.fct_orders</code> with the new code. Metabase dashboards refresh on their own cadence and show the new data.</p>
<p>If the DAG fails (tests didn't pass against real data, for example), <code class="inline">marts.fct_orders</code> stays unchanged — Metabase keeps showing the last good version until someone fixes and re-merges. Failing safely is the whole point.</p>

<h2 id="naming">Naming conventions</h2>
<table>
  <thead><tr><th>Schema name</th><th>Purpose</th><th>Who writes</th></tr></thead>
  <tbody>
    <tr><td><code class="inline">marts</code></td><td>Production — what stakeholders see</td><td>The nightly DAG, from merged main</td></tr>
    <tr><td><code class="inline">raw</code></td><td>Airbyte-landed raw data</td><td>Airbyte only</td></tr>
    <tr><td><code class="inline">dev_&lt;username&gt;</code></td><td>Personal development schema</td><td>That engineer</td></tr>
    <tr><td><code class="inline">staging_&lt;ticket&gt;</code></td><td>Shared WIP for a multi-person feature</td><td>Feature team</td></tr>
    <tr><td><code class="inline">test_&lt;something&gt;</code></td><td>CI test runs (ephemeral)</td><td>CI pipelines, auto-dropped after</td></tr>
  </tbody>
</table>

<h2 id="gotchas">Gotchas</h2>
<ul>
  <li><strong>profiles.yml in the repo.</strong> Do not commit it. Commit <code class="inline">profiles.yml.example</code> as a template and list <code class="inline">profiles.yml</code> in <code class="inline">.gitignore</code>.</li>
  <li><strong>Forgotten dev schemas.</strong> Old engineers leave; their dev schemas accumulate. Periodically run <code class="inline">DROP SCHEMA dev_&lt;leaver&gt; CASCADE;</code>.</li>
  <li><strong>Running <code class="inline">--target prod</code> locally by mistake.</strong> Painful when it happens. Defend against it by giving the dbt_user only SELECT on <code class="inline">marts</code> for developer-facing PostgreSQL roles; the nightly DAG uses a different user with write access.</li>
  <li><strong>Schema per branch</strong> (Snowflake-only pattern) doesn't translate to PostgreSQL. Stick with per-engineer schemas.</li>
</ul>
""",
    ),

    # ---- Services group ----
    Page(
        path="services/airbyte.html",
        title="Airbyte",
        h1="Airbyte",
        lede="Connector-based ingestion. Pulls data from 300+ sources into MinIO and PostgreSQL on the schedule you configure.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Services"), (None, "Airbyte")],
        toc=[
            ("tier", "Tier"),
            ("role", "Role in the pipeline"),
            ("configuration", "Configuration"),
            ("adding-sources", "Adding a new source"),
            ("troubleshooting", "Troubleshooting"),
        ],
        body="""\
<h2 id="tier">Tier</h2>
<p><strong>Cold.</strong> Airbyte runs during the ingestion window (scheduled by Airflow) and stops when the sync completes. Memory during a run: ~1.5–2 GB depending on dataset size.</p>

<h2 id="role">Role in the pipeline</h2>
<p>Airbyte is the first step of every scheduled run. It reads from your external source (database, API, CSV drop, SaaS tool) and writes landed records to two destinations in parallel:</p>
<ul>
  <li><strong>MinIO</strong> under <code class="inline">s3://OrcheStack/raw/&lt;source&gt;/&lt;timestamp&gt;/</code> — the data lake archive for ML/DS consumers.</li>
  <li><strong>PostgreSQL</strong> in the <code class="inline">raw</code> schema — the staging area for dbt.</li>
</ul>

<h2 id="configuration">Configuration</h2>
<p>OrcheStack collects Airbyte's <strong>internal PostgreSQL credentials</strong> during setup — Airbyte uses its own database for tracking sync state. This is separate from your warehouse PostgreSQL.</p>
<ul>
  <li><code class="inline">AIRBYTE_DB_HOST</code> — usually <code class="inline">postgres</code></li>
  <li><code class="inline">AIRBYTE_DB_USER</code>, <code class="inline">AIRBYTE_DB_PASSWORD</code></li>
  <li><code class="inline">AIRBYTE_DB_NAME</code> — typically <code class="inline">airbyte</code></li>
</ul>
<p>Source credentials (for the systems Airbyte pulls FROM) are entered inside Airbyte's own UI after the first sync is configured.</p>

<h2 id="adding-sources">Adding a new source</h2>
<p>Click <strong>Open Airbyte</strong> from the dashboard. This spins up the container (if cold), then opens Airbyte's web UI at <code class="inline">/app/airbyte</code>.</p>
<ol>
  <li>Click <strong>Sources → + new source</strong>.</li>
  <li>Choose the connector type (PostgreSQL, Google Sheets, Stripe, etc.).</li>
  <li>Enter the source credentials.</li>
  <li>Configure sync frequency and tables.</li>
  <li>Airbyte writes to both MinIO and PostgreSQL automatically — OrcheStack pre-wires the destinations.</li>
</ol>

<h2 id="troubleshooting">Troubleshooting</h2>
<ul>
  <li><strong>Sync fails with "connection refused"</strong> — the source system is unreachable from the Docker network. Check firewall rules and whether the source needs a VPN.</li>
  <li><strong>"Schema change detected"</strong> — the source added or removed a column. Airbyte's behaviour depends on your sync mode; see the Airbyte docs for schema evolution.</li>
  <li><strong>Airbyte won't start</strong> — its internal PostgreSQL credentials are wrong. Edit the config in the dashboard.</li>
</ul>
""",
    ),

    Page(
        path="services/airflow.html",
        title="Apache Airflow",
        h1="Apache Airflow",
        lede="The orchestration backbone. Schedules pipeline runs, coordinates dependencies between cold services, and provides a web UI for monitoring execution.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Services"), (None, "Apache Airflow")],
        toc=[
            ("tier", "Tier"),
            ("role", "Role in the pipeline"),
            ("configuration", "Configuration"),
            ("dags-live-where", "Where DAGs live"),
            ("default-dag", "The default nightly DAG"),
        ],
        body="""\
<h2 id="tier">Tier</h2>
<p>Split: <strong>scheduler and webserver are hot</strong>; <strong>workers are cold</strong>. The scheduler is the clock that triggers everything, so it must always be running. Workers only start when a task needs to run.</p>

<h2 id="role">Role in the pipeline</h2>
<p>Airflow is the thing that fires on schedule (e.g., 02:00 nightly) and tells Airbyte, dbt, and the quality tools when to run. It holds the dependency graph of your pipeline and blocks downstream tasks if an upstream one fails.</p>

<h2 id="configuration">Configuration</h2>
<p>OrcheStack configures Airflow to use the base-install PostgreSQL for its metadata database. You won't normally edit these fields:</p>
<ul>
  <li><code class="inline">AIRFLOW__DATABASE__SQL_ALCHEMY_CONN</code> — auto-generated</li>
  <li><code class="inline">AIRFLOW__CORE__EXECUTOR</code> — <code class="inline">LocalExecutor</code> for single-host deployments</li>
  <li><code class="inline">AIRFLOW__CORE__FERNET_KEY</code> — generated at install; encrypts connection credentials</li>
</ul>

<h2 id="dags-live-where">Where DAGs live</h2>
<p>DAG Python files are mounted into the Airflow container from <code class="inline">./dags/</code> on the host. Edit files there, commit to source control, and Airflow auto-discovers them within 30 seconds.</p>

<h2 id="default-dag">The default nightly DAG</h2>
<p>OrcheStack creates <code class="inline">OrcheStack_nightly</code> on first configuration. It runs at 02:00 local time and executes:</p>
<pre>airbyte_sync  →  dbt_build  →  great_expectations_check
                                         ↓
                                  elementary_report</pre>
<p>Open Airflow from the dashboard to inspect DAG runs, retry failures, and pause schedules.</p>
""",
    ),

    Page(
        path="services/dbt.html",
        title="dbt Core",
        h1="dbt Core",
        lede="The transformation layer of OrcheStack. Reads raw landed data from PostgreSQL, builds analytics-ready marts in the same database.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Services"), (None, "dbt Core")],
        toc=[
            ("tier", "Tier"),
            ("role", "Role in the pipeline"),
            ("configuration", "Configuration"),
            ("where-models-live", "Where your models live"),
            ("running-dbt", "Running dbt"),
            ("observability", "Observability"),
            ("troubleshooting", "Troubleshooting"),
        ],
        body="""\
<h2 id="tier">Tier</h2>
<p><strong>Cold.</strong> dbt Core is a CLI that runs as a scheduled task or on manual trigger — it doesn't stay running between invocations. Typical memory during a run: ~500 MB. Typical run duration: 30 seconds to 5 minutes depending on model count.</p>

<h2 id="role">Role in the pipeline</h2>
<p>Airbyte lands raw data into the <code class="inline">raw</code> schema. dbt reads from there, applies SQL transformations (staging → intermediate → marts), and writes results back to the <code class="inline">marts</code> schema in the same PostgreSQL instance. Metabase then queries <code class="inline">marts</code> for dashboards.</p>

<h2 id="configuration">Configuration</h2>
<p>OrcheStack's credentials form collects:</p>
<ul>
  <li><strong>Profile target</strong> — typically <code class="inline">dev</code> or <code class="inline">prod</code></li>
  <li><strong>Warehouse host</strong> — usually <code class="inline">postgres</code> (OrcheStack's internal service name)</li>
  <li><strong>Warehouse port</strong> — <code class="inline">5432</code></li>
  <li><strong>Warehouse database</strong> — the DB holding raw and marts schemas</li>
  <li><strong>Schema for dbt outputs</strong> — typically <code class="inline">marts</code></li>
  <li><strong>dbt user</strong> — a PostgreSQL user with read on raw, write on marts</li>
  <li><strong>dbt password</strong> — stored in <code class="inline">.env</code>, referenced in <code class="inline">profiles.yml</code></li>
</ul>
<p>OrcheStack generates <code class="inline">profiles.yml</code> at <code class="inline">./config/dbt/profiles.yml</code> during save. This file references environment variables so credentials never get committed.</p>

<h2 id="where-models-live">Where your models live</h2>
<p>Your dbt project directory is mounted into the dbt container at <code class="inline">/dbt</code>. The default project skeleton ships with a <code class="inline">staging/</code> and <code class="inline">marts/</code> folder layout, a sample <code class="inline">dbt_project.yml</code>, and a smoke test.</p>
<pre>./dbt/
├── dbt_project.yml
├── models/
│   ├── staging/
│   │   └── stg_orders.sql
│   └── marts/
│       └── fct_sales.sql
├── tests/
└── profiles.yml   # generated — do not edit by hand</pre>

<h2 id="running-dbt">Running dbt</h2>

<h3 id="scheduled">Scheduled</h3>
<p>The default Airflow DAG (<code class="inline">OrcheStack_nightly</code>) runs dbt every day at 02:00. OrcheStack creates it automatically when dbt is selected during setup.</p>

<h3 id="manual">Manual</h3>
<p>From the OrcheStack admin dashboard, click <strong>Run dbt now</strong>. The orchestrator spins up a dbt container, runs <code class="inline">dbt run --select tag:daily</code>, captures logs to PostgreSQL, and stops the container.</p>

<h2 id="observability">Observability</h2>
<p>If you enabled <strong>Elementary</strong> during setup, its container runs immediately after each dbt invocation to record test results and detect anomalies. Elementary dashboards are accessible from <code class="inline">/app/elementary</code> via the reverse proxy.</p>
<div class="callout">
  <p><strong>Tip.</strong> dbt plays best when combined with Great Expectations for assertion-based quality checks on the <code class="inline">marts</code> schema. Configure both during setup and OrcheStack wires them into the Airflow DAG automatically.</p>
</div>

<h2 id="troubleshooting">Troubleshooting</h2>
<p>dbt run failed? Three common causes:</p>
<ul>
  <li><strong>Missing raw data.</strong> Airbyte didn't land anything. Check Airflow logs for the preceding <code class="inline">airbyte_sync</code> task.</li>
  <li><strong>Permission denied on marts schema.</strong> Your dbt user lacks <code class="inline">CREATE</code> privilege on the marts schema. Fix via pgAdmin or the dashboard credential editor.</li>
  <li><strong>SQL error in a model.</strong> The dashboard's <strong>Logs</strong> tab shows the full dbt output with the failing model highlighted.</li>
</ul>
""",
    ),

    Page(
        path="services/postgres.html",
        title="PostgreSQL",
        h1="PostgreSQL",
        lede="The warehouse and the backbone of the control plane. One instance, multiple schemas, responsible for platform state plus analytics data.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Services"), (None, "PostgreSQL")],
        toc=[
            ("tier", "Tier"),
            ("role", "Role"),
            ("schemas", "Schema layout"),
            ("configuration", "Configuration"),
        ],
        body="""\
<h2 id="tier">Tier</h2>
<p><strong>Hot.</strong> PostgreSQL is part of the base install and stays running at all times. Expected memory: ~800 MB–1.2 GB depending on warehouse size.</p>

<h2 id="role">Role</h2>
<p>Everything writable lands here. The same PostgreSQL instance holds:</p>
<ul>
  <li><strong>Platform state</strong> — user accounts, roles, permissions, sessions, audit log.</li>
  <li><strong>Warehouse raw</strong> — landed data from Airbyte, awaiting dbt.</li>
  <li><strong>Warehouse marts</strong> — dbt outputs served to Metabase.</li>
  <li><strong>Service metadata</strong> — Airflow's DAG state, OpenMetadata's catalogue (when enabled).</li>
</ul>
<p>Each concern gets its own schema. Separation makes permissions cleaner without adding operational complexity.</p>

<h2 id="schemas">Schema layout</h2>
<pre>platform       -- users, roles, sessions, audit
raw            -- Airbyte-landed tables, one per source
marts          -- dbt outputs: fct_*, dim_*, stg_*
airflow        -- Airflow's internal metadata tables
openmetadata   -- OpenMetadata's internal tables (if enabled)</pre>

<h2 id="configuration">Configuration</h2>
<p>Credentials are collected at install time:</p>
<ul>
  <li><code class="inline">POSTGRES_USER</code> — superuser for platform management</li>
  <li><code class="inline">POSTGRES_PASSWORD</code> — bcrypt-stored as platform password; stored plain in <code class="inline">.env</code> for Postgres</li>
  <li><code class="inline">POSTGRES_DB</code> — default database name (typically <code class="inline">OrcheStack</code>)</li>
</ul>
<p>OrcheStack creates per-service PostgreSQL roles with scoped privileges so each downstream tool (dbt, Airflow, Metabase) authenticates as its own user with only the permissions it needs.</p>
""",
    ),

    Page(
        path="services/minio.html",
        title="MinIO",
        h1="MinIO",
        lede="S3-compatible object storage. The data lake layer — lands raw files alongside the warehouse, stays reachable for ML and DS consumers.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Services"), (None, "MinIO")],
        toc=[
            ("tier", "Tier"),
            ("role", "Role"),
            ("buckets", "Bucket layout"),
            ("configuration", "Configuration"),
            ("accessing-externally", "Accessing externally"),
        ],
        body="""\
<h2 id="tier">Tier</h2>
<p><strong>Hot.</strong> MinIO is lightweight (~200 MB) and stays running so external consumers (ML notebooks, DS pipelines, ad-hoc analysis) can access raw files at any time.</p>

<h2 id="role">Role</h2>
<p>MinIO is the "open end" of OrcheStack. Airbyte writes every sync to MinIO in parallel with PostgreSQL. The warehouse track (Airbyte → Postgres → dbt → Metabase) serves BI; the lake track (Airbyte → MinIO → external consumers) serves ML/DS use cases.</p>
<p>MinIO is never upstream of dbt. dbt only reads from PostgreSQL.</p>

<h2 id="buckets">Bucket layout</h2>
<pre>OrcheStack/
├── raw/                  # Airbyte-landed files, partitioned by date
│   └── &lt;source&gt;/
│       └── &lt;YYYY-MM-DD&gt;/
├── backups/              # nightly dumps of PostgreSQL
└── exports/              # user-triggered data exports from the dashboard</pre>

<h2 id="configuration">Configuration</h2>
<ul>
  <li><code class="inline">MINIO_ROOT_USER</code> — administrator access key</li>
  <li><code class="inline">MINIO_ROOT_PASSWORD</code> — administrator secret key</li>
  <li><code class="inline">MINIO_BUCKET</code> — top-level bucket name (default: <code class="inline">OrcheStack</code>)</li>
</ul>

<h2 id="accessing-externally">Accessing externally</h2>
<p>MinIO exposes the standard S3 API on <code class="inline">:9000</code>. Point any S3 client at <code class="inline">http://&lt;your-host&gt;:9000</code> with the root credentials, or generate service accounts from MinIO's web UI at <code class="inline">/app/minio</code>.</p>
""",
    ),

    Page(
        path="services/metabase.html",
        title="Metabase",
        h1="Metabase",
        lede="The BI layer of OrcheStack. Dashboards for stakeholders, SQL workbench for power users — all querying the marts schema directly.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Services"), (None, "Metabase")],
        toc=[
            ("tier", "Tier"),
            ("role", "Role"),
            ("configuration", "Configuration"),
            ("first-dashboard", "Your first dashboard"),
        ],
        body="""\
<h2 id="tier">Tier</h2>
<p><strong>Hot.</strong> Metabase stays running whenever it's enabled. Stakeholders need to open dashboards at any time — business hours, board meetings, ad-hoc exploration. Memory: ~1–1.5 GB.</p>

<h2 id="role">Role</h2>
<p>Metabase reads from the <code class="inline">marts</code> schema in PostgreSQL — the curated output of dbt. It does not read from <code class="inline">raw</code> (that's dbt's job) and it doesn't write anywhere.</p>

<h2 id="configuration">Configuration</h2>
<ul>
  <li><strong>Admin email</strong> — the first Metabase user you create on its own first-run wizard.</li>
  <li><strong>Admin password</strong> — set during first-run setup.</li>
  <li><strong>Warehouse connection</strong> — OrcheStack pre-populates this with the PostgreSQL details and a scoped <code class="inline">metabase</code> user that has read-only access to <code class="inline">marts</code>.</li>
</ul>
<div class="callout">
  <p><strong>Note.</strong> Metabase maintains its own internal application database separate from your warehouse PostgreSQL. OrcheStack stores Metabase's internal state in PostgreSQL too, under the <code class="inline">metabase_app</code> schema, so your backups cover it.</p>
</div>

<h2 id="first-dashboard">Your first dashboard</h2>
<p>Open Metabase from the dashboard. After the first-run wizard completes:</p>
<ol>
  <li>Click <strong>Browse data</strong> → your warehouse → <code class="inline">marts</code> schema.</li>
  <li>Pick a table (e.g. <code class="inline">fct_sales</code>).</li>
  <li>Use the query builder or SQL editor to explore.</li>
  <li>Save the question, add it to a dashboard.</li>
</ol>
""",
    ),

    # ---- Lighter service pages (still properly structured) ----
    Page(
        path="services/openmetadata.html",
        title="OpenMetadata",
        h1="OpenMetadata",
        lede="Column-level lineage, data catalog, glossary. Auto-ingests from Airbyte, dbt, and PostgreSQL to show how data flows end to end.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Services"), (None, "OpenMetadata")],
        toc=[
            ("tier", "Tier"),
            ("role", "Role"),
            ("configuration", "Configuration"),
            ("when-to-use", "When to open it"),
        ],
        body="""\
<h2 id="tier">Tier</h2>
<p><strong>Cold.</strong> OpenMetadata is heavy (~2–3 GB RAM) and stakeholders rarely open it. Engineers open it when they need to understand lineage — OrcheStack spins it up on click and stops it when idle.</p>

<h2 id="role">Role</h2>
<p>OpenMetadata pulls metadata from every stage (Airbyte sources, PostgreSQL schemas, dbt models, Metabase dashboards) to produce column-level lineage graphs. It answers the question: "If I change this column, what breaks downstream?"</p>

<h2 id="configuration">Configuration</h2>
<ul>
  <li><strong>Admin email and password</strong> — for the OpenMetadata web UI</li>
  <li><strong>JWT secret</strong> — 32+ characters, used for internal API authentication</li>
  <li><strong>Backend PostgreSQL</strong> — OrcheStack stores OpenMetadata's state in the <code class="inline">openmetadata</code> schema of the base PostgreSQL</li>
</ul>

<h2 id="when-to-use">When to open it</h2>
<p>Typical triggers: before a schema change (to check downstream impact), after a pipeline failure (to see which models are affected), during an audit (to document data provenance).</p>
""",
    ),

    Page(
        path="services/great-expectations.html",
        title="Great Expectations",
        h1="Great Expectations",
        lede="Declarative data quality. Runs after every dbt build. Failed expectations block downstream tasks.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Services"), (None, "Great Expectations")],
        toc=[
            ("tier", "Tier"),
            ("role", "Role"),
            ("writing-expectations", "Writing expectations"),
        ],
        body="""\
<h2 id="tier">Tier</h2>
<p><strong>Cold.</strong> Runs as a post-dbt task inside the scheduled DAG. Memory during a run: ~500 MB.</p>

<h2 id="role">Role</h2>
<p>Great Expectations runs assertions against the <code class="inline">marts</code> schema after dbt finishes. If any expectation fails, downstream tasks (Elementary reporting, dashboard refresh) are blocked and the DAG marks the run as failed.</p>

<h2 id="writing-expectations">Writing expectations</h2>
<p>Expectations live in <code class="inline">./great_expectations/expectations/</code> as YAML files, one per mart. Example:</p>
<pre>expectations:
  - expect_column_values_to_not_be_null:
      column: customer_id
  - expect_column_values_to_be_between:
      column: total_amount
      min_value: 0
      max_value: 10000000</pre>
""",
    ),

    Page(
        path="services/pgadmin.html",
        title="pgAdmin",
        h1="pgAdmin",
        lede="The PostgreSQL web UI for engineers — inspect tables, write ad-hoc SQL, review query plans. Starts on click, stops when idle.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Services"), (None, "pgAdmin")],
        toc=[
            ("tier", "Tier"),
            ("role", "Role"),
            ("configuration", "Configuration"),
            ("who-has-access", "Who has access"),
        ],
        body="""\
<h2 id="tier">Tier</h2>
<p><strong>Cold.</strong> pgAdmin is ~500 MB when running and is used ad-hoc, so it doesn't need to stay hot. Click <strong>Open pgAdmin</strong> in the dashboard — OrcheStack spins it up, you do your work, close your session, and it stops after the idle timeout.</p>

<h2 id="role">Role</h2>
<p>Engineer-only tool for exploring the warehouse. Stakeholders use Metabase; engineers use pgAdmin when they need raw SQL or schema inspection. Alternatives: <strong>Adminer</strong> (~50 MB, single-file PHP) or <strong>pgweb</strong> (~30 MB, read-only Go binary).</p>

<h2 id="configuration">Configuration</h2>
<ul>
  <li><code class="inline">PGADMIN_DEFAULT_EMAIL</code> — admin login email</li>
  <li><code class="inline">PGADMIN_DEFAULT_PASSWORD</code> — admin password</li>
</ul>
<p>On first launch, add a server connection pointing at <code class="inline">postgres:5432</code> with your warehouse credentials.</p>

<h2 id="who-has-access">Who has access</h2>
<p>By default, only users with roles that have <code class="inline">can_start=true</code> and <code class="inline">can_use=true</code> on pgAdmin. That's typically Admin and Engineer. Analysts can be granted access per-role from the dashboard's <strong>Users → Roles</strong> page.</p>
""",
    ),

    # ---- Operations group ----
    Page(
        path="credentials.html",
        title="Managing credentials",
        h1="Managing credentials",
        lede="How OrcheStack stores service credentials, how to rotate them safely, and what breaks if you edit them carelessly.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Operations"), (None, "Managing credentials")],
        toc=[
            ("where-stored", "Where credentials are stored"),
            ("rotating", "Rotating a credential"),
            ("container-recreation", "Container recreation"),
            ("production-hardening", "Production hardening"),
        ],
        body="""\
<h2 id="where-stored">Where credentials are stored</h2>
<p>Every service credential (database passwords, API keys, admin emails) lives in <code class="inline">./config/.env</code> on the host. This file is <code class="inline">chmod 600</code>-protected and should be added to your <code class="inline">.gitignore</code> immediately.</p>
<p>The generated <code class="inline">docker-compose.yml</code> references credentials as <code class="inline">${VAR_NAME}</code> — at container start, Docker Compose substitutes values from <code class="inline">.env</code>.</p>

<h2 id="rotating">Rotating a credential</h2>
<p>From the OrcheStack admin dashboard:</p>
<ol>
  <li>Click the service tile.</li>
  <li>Click <strong>Edit config</strong>.</li>
  <li>Update the field. OrcheStack runs a live connection test before letting you save.</li>
  <li>Click <strong>Save &amp; apply</strong>.</li>
</ol>

<h2 id="container-recreation">Container recreation</h2>
<p>Docker Compose semantics: <code class="inline">restart</code> reuses the existing container (stale env vars); <code class="inline">up</code> with changed <code class="inline">.env</code> values detects a config hash diff and recreates. OrcheStack issues <code class="inline">docker compose up -d --force-recreate &lt;service&gt;</code> on save, which picks up the new values.</p>
<p>Data volumes are preserved across recreates — you don't lose history when rotating a password.</p>

<h2 id="production-hardening">Production hardening</h2>
<p><code class="inline">.env</code> is the MVP approach. For production:</p>
<ul>
  <li><strong>Docker secrets</strong> — works well with Swarm mode.</li>
  <li><strong>HashiCorp Vault</strong> — for dynamic credentials with TTLs.</li>
  <li><strong>SOPS + age</strong> — encrypted <code class="inline">.env</code> files safe to commit.</li>
</ul>
<p>These are documented in the <em>Advanced deployment</em> section (roadmap), not wired into the default OrcheStack install.</p>
""",
    ),

    Page(
        path="backup-restore.html",
        title="Backup & restore",
        h1="Backup & restore",
        lede="What to back up, how often, and how to restore a OrcheStack instance from scratch.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Operations"), (None, "Backup & restore")],
        toc=[
            ("what-to-backup", "What to back up"),
            ("nightly-job", "The nightly backup job"),
            ("manual-backup", "Manual backup"),
            ("restore", "Restoring from backup"),
        ],
        body="""\
<h2 id="what-to-backup">What to back up</h2>
<p>Three things matter:</p>
<ol>
  <li><strong>PostgreSQL</strong> — a <code class="inline">pg_dump</code> of the entire instance covers platform state, raw, marts, Airflow, and OpenMetadata metadata.</li>
  <li><strong>MinIO bucket</strong> — use <code class="inline">mc mirror</code> to replicate the <code class="inline">OrcheStack/</code> bucket to external storage.</li>
  <li><strong>Config directory</strong> — <code class="inline">./config/</code> contains your <code class="inline">.env</code>, generated <code class="inline">docker-compose.yml</code>, and any custom DAGs or dbt models.</li>
</ol>

<h2 id="nightly-job">The nightly backup job</h2>
<p>The default Airflow DAG <code class="inline">OrcheStack_backup_nightly</code> runs at 01:30 local time and:</p>
<ol>
  <li>Runs <code class="inline">pg_dump</code> and writes the output to <code class="inline">s3://OrcheStack/backups/&lt;date&gt;/postgres.sql.gz</code>.</li>
  <li>Runs <code class="inline">tar czf</code> on <code class="inline">./config/</code> and uploads to the same prefix.</li>
  <li>Deletes backups older than 30 days (configurable).</li>
</ol>

<h2 id="manual-backup">Manual backup</h2>
<p>From the OrcheStack admin dashboard, go to <strong>Operations → Backup now</strong>. Triggers the same Airflow task immediately.</p>

<h2 id="restore">Restoring from backup</h2>
<p>On a fresh OrcheStack install:</p>
<ol>
  <li>Stop the stack: <code class="inline">docker compose down</code>.</li>
  <li>Restore <code class="inline">./config/</code> from backup.</li>
  <li>Start the stack: <code class="inline">docker compose up -d</code>.</li>
  <li>Run <code class="inline">pg_restore</code> against the PostgreSQL container.</li>
  <li>Run <code class="inline">mc mirror</code> in reverse to restore MinIO.</li>
</ol>
<p>A full restore runbook with exact commands is in the <a href="troubleshooting.html">Troubleshooting</a> section.</p>
""",
    ),

    Page(
        path="upgrading.html",
        title="Upgrading OrcheStack",
        h1="Upgrading OrcheStack",
        lede="How to safely move from one OrcheStack version to the next without losing data or breaking your pipelines.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Operations"), (None, "Upgrading OrcheStack")],
        toc=[
            ("versioning", "Versioning"),
            ("upgrade-steps", "Upgrade steps"),
            ("rollback", "Rollback"),
        ],
        body="""\
<h2 id="versioning">Versioning</h2>
<p>OrcheStack follows semantic versioning. Docker images are pinned by tag in <code class="inline">docker-compose.yml</code> — never use <code class="inline">:latest</code> in production.</p>

<h2 id="upgrade-steps">Upgrade steps</h2>
<ol>
  <li>Read the release notes at <a href="https://github.com/tripleaceme/OrcheStack/releases">github.com/tripleaceme/OrcheStack/releases</a> for breaking changes.</li>
  <li>Take a full backup (see <a href="backup-restore.html">Backup &amp; restore</a>).</li>
  <li>Edit <code class="inline">docker-compose.yml</code> to bump image tags.</li>
  <li>Run <code class="inline">docker compose pull</code> to fetch the new images.</li>
  <li>Run <code class="inline">docker compose up -d</code>. Services with migrations will run them at startup.</li>
  <li>Verify the dashboard loads and the nightly DAG succeeds on its next run.</li>
</ol>

<h2 id="rollback">Rollback</h2>
<p>If something breaks: revert <code class="inline">docker-compose.yml</code> to the previous tags, restore from backup, and file an issue.</p>
""",
    ),

    Page(
        path="troubleshooting.html",
        title="Troubleshooting",
        h1="Troubleshooting",
        lede="Common issues and their fixes. Start here before opening a support ticket.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Operations"), (None, "Troubleshooting")],
        toc=[
            ("cant-reach-ui", "Can't reach the UI"),
            ("pipeline-failed", "Pipeline failed"),
            ("service-stuck", "A service is stuck"),
            ("logs", "Where logs live"),
        ],
        body="""\
<h2 id="cant-reach-ui">Can't reach the UI</h2>
<p>If <code class="inline">http://localhost</code> doesn't respond:</p>
<ul>
  <li>Check the proxy container: <code class="inline">docker compose ps orchestack-proxy</code>.</li>
  <li>Check port 80 isn't bound by another process: <code class="inline">sudo lsof -i :80</code>.</li>
  <li>Check proxy logs: <code class="inline">docker compose logs orchestack-proxy</code>.</li>
</ul>

<h2 id="pipeline-failed">Pipeline failed</h2>
<p>Open the Airflow UI → find the failed DAG run → click the red task → <strong>View logs</strong>. The error message will pinpoint whether it's a source connectivity issue (Airbyte), a SQL error (dbt), or a quality check failure (Great Expectations).</p>

<h2 id="service-stuck">A service is stuck</h2>
<p>If a cold service won't stop (e.g. OpenMetadata stays running for hours):</p>
<ol>
  <li>Check <code class="inline">platform.service_sessions</code> for stale sessions that aren't being garbage-collected.</li>
  <li>From the dashboard, <strong>Force stop</strong> the service (requires <code class="inline">can_force_stop</code>).</li>
  <li>If that fails, <code class="inline">docker compose stop &lt;service&gt;</code> from the host.</li>
</ol>

<h2 id="logs">Where logs live</h2>
<ul>
  <li><strong>Service logs</strong>: <code class="inline">docker compose logs -f &lt;service&gt;</code></li>
  <li><strong>Pipeline logs</strong>: Airflow UI, or <code class="inline">./config/airflow/logs/</code></li>
  <li><strong>Platform audit log</strong>: <code class="inline">platform.audit_log</code> table</li>
  <li><strong>dbt run logs</strong>: The dashboard <strong>Logs</strong> tab</li>
</ul>
""",
    ),

    Page(
        path="cli.html",
        title="CLI commands",
        h1="CLI commands",
        lede="The <code class=\"inline\">OrcheStack</code> command-line tool for operators who prefer the terminal over the UI.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Reference"), (None, "CLI commands")],
        toc=[
            ("install-cli", "Install the CLI"),
            ("commands", "Commands"),
        ],
        body="""\
<h2 id="install-cli">Install the CLI</h2>
<pre>curl -fsSL https://orchestack.africa/cli | sh</pre>
<p>The CLI is a thin wrapper around the OrcheStack REST API and <code class="inline">docker compose</code>. It runs on the host and needs access to the Docker socket.</p>

<h2 id="commands">Commands</h2>
<pre>OrcheStack status                     # show state of all services
OrcheStack services list              # list configured services
OrcheStack run &lt;dag&gt;                  # trigger an Airflow DAG
OrcheStack logs &lt;service&gt;              # stream logs
OrcheStack users list                 # list platform users
OrcheStack backup                     # trigger a manual backup
OrcheStack version                    # print platform and CLI versions</pre>
<p>Full reference with flags and examples: <code class="inline">OrcheStack --help</code>.</p>
""",
    ),

    Page(
        path="api.html",
        title="REST API",
        h1="REST API",
        lede="The dashboard exposes a small REST API for automation and external integrations. Authenticate with a bearer token.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Reference"), (None, "REST API")],
        toc=[
            ("auth", "Authentication"),
            ("endpoints", "Core endpoints"),
        ],
        body="""\
<h2 id="auth">Authentication</h2>
<p>Generate an API token from <strong>Account settings → API tokens</strong> in the dashboard. Include it on every request:</p>
<pre>Authorization: Bearer &lt;your-token&gt;</pre>

<h2 id="endpoints">Core endpoints</h2>
<pre>GET  /app/api/services          # list services and their status
POST /app/api/services/{id}/start   # start a cold service
POST /app/api/services/{id}/stop    # end the caller's session
POST /app/api/pipelines/{dag}/run   # trigger a DAG run
GET  /app/api/users             # list users (admin only)
POST /app/api/users             # create a user (admin only)
GET  /app/api/audit             # query the audit log</pre>
<p>Responses are JSON. Rate limit is 60 requests/minute per token.</p>
""",
    ),

    Page(
        path="compose-reference.html",
        title="docker-compose reference",
        h1="docker-compose reference",
        lede="Structure of the docker-compose.yml that OrcheStack generates from your service selection, with common overrides documented.",
        breadcrumb=[("index.html", "Docs"), ("index.html", "Reference"), (None, "docker-compose reference")],
        toc=[
            ("base-services", "Base services"),
            ("profiles", "Profiles"),
            ("overrides", "Common overrides"),
        ],
        body="""\
<h2 id="base-services">Base services</h2>
<p>The generated compose always includes:</p>
<ul>
  <li><code class="inline">orchestack-proxy</code> — reverse proxy (Traefik)</li>
  <li><code class="inline">orchestack-auth</code> — nginx serving signup, login and the setup wizard</li>
  <li><code class="inline">orchestack-dashboard</code> — administrator dashboard</li>
  <li><code class="inline">orchestack-postgres</code> — PostgreSQL warehouse + platform metadata store</li>
  <li><code class="inline">orchestack-orchestrator</code> — Python service-lifecycle manager</li>
</ul>

<h2 id="profiles">Profiles</h2>
<p>Each selectable tool is added under a Docker Compose profile matching its name. <code class="inline">docker compose --profile airbyte up -d</code> starts the Airbyte stack. OrcheStack manages profile activation automatically via the orchestrator.</p>

<h2 id="overrides">Common overrides</h2>
<p>Put custom overrides in <code class="inline">docker-compose.override.yml</code> (not edited by OrcheStack):</p>
<pre>services:
  orchestack-postgres:
    deploy:
      resources:
        limits:
          memory: 2g
  orchestack-dashboard:
    environment:
      - DASHBOARD_LOG_LEVEL=info</pre>
""",
    ),
]


# =============================================================================
# 3. Rendering
# =============================================================================

def relpath(from_docs_root: str, to_docs_root: str) -> str:
    """Relative href from one page (directory) to another.

    Both inputs are paths relative to DOCS root.
    """
    from_dir = os.path.dirname(from_docs_root)
    rel = os.path.relpath(to_docs_root, from_dir or ".")
    # os.path.relpath returns '.' for same-file-in-same-dir; we want just the filename
    return rel if rel != "." else to_docs_root


def up_prefix(depth: int) -> str:
    """Prefix to escape from a docs page back to ROOT (where index.html and assets/ live).

    docs/install.html (depth=0) needs `../` to reach ROOT.
    docs/services/airbyte.html (depth=1) needs `../../` to reach ROOT.
    """
    return "../" * (depth + 1)  # +1 because every docs page is at least one level under ROOT (inside docs/)


def page_depth(path: str) -> int:
    """0 for direct children of docs/ (e.g. install.html), 1 for docs/services/x.html."""
    return path.count("/")


def render_sidebar(current: str) -> str:
    """Render the canonical sidebar with `current` marked active.

    current: page path relative to DOCS root, e.g. 'services/dbt.html'.
    """
    depth = page_depth(current)
    lines = ['  <aside class="docs-sidebar">']
    for group_title, pages in SIDEBAR:
        lines.append('    <div class="group">')
        lines.append(f'      <div class="group-title">{group_title}</div>')
        lines.append('      <ul>')
        for href_from_root, label in pages:
            rel = relpath(current, href_from_root)
            active = ' class="active"' if href_from_root == current else ''
            lines.append(f'        <li><a{active} href="{rel}">{label}</a></li>')
        lines.append('      </ul>')
        lines.append('    </div>')
    lines.append('  </aside>')
    return "\n".join(lines)


def render_breadcrumb(crumbs: list[tuple[str | None, str]], current: str) -> str:
    """Render the breadcrumb strip."""
    parts = []
    for href, label in crumbs:
        if href is None:
            parts.append(label)
        else:
            rel = relpath(current, href)
            parts.append(f'<a href="{rel}">{label}</a>')
    return " › ".join(parts)


def render_toc(toc: list[tuple[str, str]]) -> str:
    if not toc:
        return ""
    lines = ['  <aside class="docs-toc">',
             '    <div class="toc-title">On this page</div>',
             '    <ul>']
    for i, (anchor, label) in enumerate(toc):
        active = ' class="active"' if i == 0 else ''
        lines.append(f'      <li><a{active} href="#{anchor}">{label}</a></li>')
    lines.extend(['    </ul>', '  </aside>'])
    return "\n".join(lines)


def render_header(current: str) -> str:
    depth = page_depth(current)
    up = up_prefix(depth)  # e.g. "../" for docs/install.html, "../../" for docs/services/dbt.html
    docs_rel = relpath(current, "index.html")  # href to docs/index.html from current page

    return f"""<header>
  <div class="wrap">
    <nav class="nav">
      <a href="{up}index.html" class="brand">
        <div class="brand-mark">O</div>
        <span>OrcheStack</span>
      </a>
      <div class="docs-search">
        <input type="text" placeholder="Search the docs...">
        <span class="kbd">⌘K</span>
      </div>
      <ul>
        <li><a class="nav-link" href="{up}docs/install.html">Get started</a></li>
        <li><a class="nav-link" href="{up}services.html">Services</a></li>
        <li><a class="nav-link active" href="{docs_rel}">Docs</a></li>
        <li><a class="nav-link" href="#">Blog</a></li>
        <li><a class="nav-link" href="{up}contact.html">Contact</a></li>
      </ul>
      <div class="cta">
        <span class="nav-social">
          <a class="nav-icon" href="#" aria-label="Discord" title="Discord">
            <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M20.317 4.37a19.79 19.79 0 0 0-4.885-1.515.07.07 0 0 0-.073.035c-.21.375-.444.864-.608 1.25a18.3 18.3 0 0 0-5.487 0 12.6 12.6 0 0 0-.617-1.25.07.07 0 0 0-.073-.035A19.74 19.74 0 0 0 3.683 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.08.08 0 0 0 .031.055 19.9 19.9 0 0 0 5.993 3.03.07.07 0 0 0 .076-.027c.461-.63.873-1.295 1.226-1.995a.07.07 0 0 0-.038-.098 13.1 13.1 0 0 1-1.872-.892.07.07 0 0 1-.007-.116c.126-.094.252-.192.372-.291a.07.07 0 0 1 .073-.01c3.927 1.793 8.18 1.793 12.062 0a.07.07 0 0 1 .074.009c.12.099.246.198.373.292a.07.07 0 0 1-.006.116 12.3 12.3 0 0 1-1.873.891.07.07 0 0 0-.037.099c.36.7.772 1.365 1.225 1.994a.07.07 0 0 0 .076.028 19.84 19.84 0 0 0 6.002-3.03.07.07 0 0 0 .032-.054c.5-5.177-.838-9.674-3.548-13.66a.06.06 0 0 0-.031-.028zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.956-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z"/></svg>
          </a>
          <a class="nav-icon" href="#" aria-label="GitHub" title="GitHub">
            <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 .5C5.37.5 0 5.87 0 12.5c0 5.3 3.44 9.8 8.2 11.39.6.11.8-.26.8-.58v-2.02c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.33-1.76-1.33-1.76-1.09-.74.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.83 2.8 1.3 3.49.99.11-.78.42-1.3.76-1.6-2.67-.3-5.47-1.34-5.47-5.95 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.17 0 0 1-.32 3.3 1.23.96-.27 1.98-.4 3-.41 1.02.01 2.04.14 3 .41 2.3-1.55 3.3-1.23 3.3-1.23.66 1.65.24 2.87.12 3.17.77.84 1.24 1.91 1.24 3.22 0 4.62-2.81 5.64-5.49 5.94.43.37.81 1.1.81 2.22v3.29c0 .32.2.69.81.57 4.76-1.58 8.19-6.08 8.19-11.38C24 5.87 18.63.5 12 .5z"/></svg>
          </a>
        </span>
        <a class="btn btn-primary btn-sm" href="{up}docs/install.html">Get started</a>
      </div>
    </nav>
  </div>
</header>"""


def render_page(page: Page) -> str:
    depth = page_depth(page.path)
    up = up_prefix(depth)
    css_href = f"{up}assets/css/main.css"
    header = render_header(page.path)
    sidebar = render_sidebar(page.path)
    breadcrumb_html = render_breadcrumb(page.breadcrumb, page.path) if page.breadcrumb else ""
    toc = render_toc(page.toc)

    # Title suffix: don't repeat "OrcheStack" if the page title already ends with it.
    title = page.title if "OrcheStack" in page.title else f"{page.title} — OrcheStack"

    # Next/prev navigation: infer from sidebar position.
    nav_links = _next_prev_links(page.path)

    # Body is inserted verbatim. We do NOT re-indent because whitespace inside
    # <pre> tags is semantically significant (every space renders literally).
    body = page.body.rstrip()

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="{css_href}">
</head>
<body>

{header}

<div class="docs-layout">

{sidebar}

  <main class="docs-main">
    <div class="breadcrumb">{breadcrumb_html}</div>
    <h1>{page.h1}</h1>
    <p class="lede">{page.lede}</p>

{body}

{nav_links}
  </main>

{toc}

</div>

</body>
</html>
"""


def _flat_ordered_pages() -> list[str]:
    """Return all sidebar page hrefs in sidebar order (used for next/prev)."""
    flat = []
    for _, pages in SIDEBAR:
        for href, _ in pages:
            flat.append(href)
    return flat


def _next_prev_links(current: str) -> str:
    flat = _flat_ordered_pages()
    if current not in flat:
        return ""
    idx = flat.index(current)
    prev_href = flat[idx - 1] if idx > 0 else None
    next_href = flat[idx + 1] if idx < len(flat) - 1 else None

    prev_label = _label_for(prev_href) if prev_href else None
    next_label = _label_for(next_href) if next_href else None

    parts = ['    <div class="next-prev">']
    if prev_href:
        rel = relpath(current, prev_href)
        parts.append(f'      <a href="{rel}">')
        parts.append('        <span class="dir">← Previous</span>')
        parts.append(f'        <span class="title">{prev_label}</span>')
        parts.append('      </a>')
    else:
        parts.append('      <span></span>')
    if next_href:
        rel = relpath(current, next_href)
        parts.append(f'      <a class="next" href="{rel}">')
        parts.append('        <span class="dir">Next</span>')
        parts.append(f'        <span class="title">{next_label} →</span>')
        parts.append('      </a>')
    else:
        parts.append('      <span></span>')
    parts.append('    </div>')
    return "\n".join(parts)


def _label_for(href: str) -> str:
    for _, pages in SIDEBAR:
        for h, label in pages:
            if h == href:
                return label
    return href


def main() -> None:
    written = 0
    for page in PAGES:
        out_path = DOCS / page.path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        html = render_page(page)
        out_path.write_text(html, encoding="utf-8")
        written += 1
        print(f"  wrote: docs/{page.path}")
    print(f"Done — {written} pages written.")


if __name__ == "__main__":
    main()
