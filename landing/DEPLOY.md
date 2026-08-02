# Deploying the Vision AI landing + waitlist to Vercel

This folder (`landing/`) is a **static site** — one self-contained `index.html`,
no build step, no backend. It's the public marketing page + email waitlist you
can put in program applications (AI Grant, YC, Antler) and investor emails.

> The full Vision AI app can't go on Vercel — it needs Ollama, a real browser
> for automation, and long-running background work. This landing page is
> deliberately separate: it explains the product and captures interest, and it
> runs anywhere static.

---

## Step 1 — wire the waitlist (2 minutes, do this first)

The form works out of the box (it shows a "email me" fallback), but to actually
**collect emails into a dashboard**, connect Formspree (free tier: 50
submissions/month, no credit card):

1. Go to [formspree.io](https://formspree.io) → sign up (free).
2. **New form** → name it "Vision AI waitlist" → set the notification email to
   `k.likhit2007@gmail.com`.
3. Copy the form's endpoint — it looks like `https://formspree.io/f/abcdwxyz`.
4. Open `index.html`, find this line near the bottom (in the `<script>`):
   ```js
   const FORMSPREE_ENDPOINT = "https://formspree.io/f/REPLACE_WITH_YOUR_FORM_ID";
   ```
   Replace it with your real endpoint. Save.

That's it — every signup now lands in your Formspree inbox **and** emails you.

*(Prefer something else? Any form backend works — Getform, Tally, Google Forms.
Just swap the endpoint. Or skip it entirely: the page already falls back to
"email k.likhit2007@gmail.com to join.")*

---

## Step 2 — deploy to Vercel

### Option A — drag & drop (no tools, easiest)

1. Go to [vercel.com](https://vercel.com) → sign up / log in (GitHub login is fine).
2. **Add New… → Project → Deploy** — or use the drag-and-drop deploy at
   [vercel.com/new](https://vercel.com/new).
3. Drag **this `landing/` folder** onto the page. Vercel detects a static site
   automatically — no framework, no build command needed.
4. Click **Deploy**. In ~20 seconds you get a live URL like
   `https://vision-ai-xxxx.vercel.app`.

### Option B — Vercel CLI (if you prefer terminal)

```bash
npm i -g vercel
cd landing
vercel        # first run: log in + confirm defaults (framework: Other)
vercel --prod # promote to your production URL
```

Vercel serves `index.html` at the root automatically. No `vercel.json` needed
for a single static page.

---

## Step 3 — (optional) custom domain

In the Vercel project → **Settings → Domains** → add a domain you own
(e.g. `visionai.app`). Vercel gives you the DNS records to point at it. Until
then, the `*.vercel.app` URL is a perfectly good link for applications and
outreach.

---

## What to double-check after deploy

- Open the live URL on your phone — confirm the hero animation runs and nothing
  overflows sideways.
- Submit your own email through the waitlist form → confirm it shows up in
  Formspree and you get the notification email.
- The links in the footer (`mailto:` and LinkedIn) point at your real accounts.

---

## Updating the page later

Edit `index.html`, then redeploy (drag-drop again, or `vercel --prod`). Same URL,
new content. Keep this folder in the repo so the site is version-controlled
alongside the app.
