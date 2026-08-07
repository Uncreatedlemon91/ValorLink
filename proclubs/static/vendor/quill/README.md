# Quill 2.0.3 (vendored)

`quill.js` and `quill.snow.css` are the unmodified production build from
npm (`quill@2.0.3`, BSD-3-Clause -- see `LICENSE`), served locally rather
than from a CDN. Two reasons:

- This site has no build step (no bundler/npm install at deploy time), so
  a CDN `<script src>` would be the only way to *load* it anyway -- but
  Quill's own JS runs client-side either way, and vendoring means the
  editor keeps working even if a CDN has an outage or is blocked (as
  jsdelivr/unpkg/cdnjs are in some sandboxed environments), with one less
  third-party origin the browser has to trust.
- Quill 2.x specifically (not 1.x) is required: its list HTML changed from
  a flat `data-list` attribute structure that only renders correctly
  inside Quill's own editor styling, to real nested `<ol>`/`<ul>`/`<li>`
  markup that displays correctly on the public article page too, which
  doesn't load Quill's CSS at all (see html_sanitize.py -- the sanitized
  output has to stand on its own).

To upgrade: download a newer `quill-X.Y.Z.tgz` from
https://registry.npmjs.org/quill and replace `dist/quill.js` +
`dist/quill.snow.css` here, then sanity-check the toolbar/output HTML
still matches what proclubs/html_sanitize.py allows through.
