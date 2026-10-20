# Vacuula artwork

The integration uses the official Vacuula vampire-robot artwork from
[`app/public/favicon-vacuula.svg`](https://github.com/vacuula/vacuula/blob/fa372600e04917f81074f2e8a336d9c6a2966d1e/app/public/favicon-vacuula.svg)
at upstream commit `fa372600e04917f81074f2e8a336d9c6a2966d1e`.
The original SVG is preserved as
[`vacuula.svg`](../../custom_components/vacuula/brand/vacuula.svg). It matches the
reviewed Vacuula 1.0.0 source. The source artwork's colors, shapes and effects are
unchanged; the packaged images are rendered, trimmed and resized versions.

The artwork comes from the [Vacuula project](https://github.com/vacuula/vacuula)
under its upstream GNU Affero General Public License version 3. The complete
license is preserved in
[`LICENSE-AGPL-3.0.txt`](../../custom_components/vacuula/brand/LICENSE-AGPL-3.0.txt).
The original SVG, license and attribution notice ship with the images in
`custom_components/vacuula/brand/`, including HACS and manual installations.
The Python integration implementation is independently written. Product names
and artwork identify the connected project and do not imply its endorsement.

## Packaged images

| File | Dimensions | Format |
| --- | --- | --- |
| `custom_components/vacuula/brand/icon.png` | 256 × 256 | Transparent, lossless, interlaced RGBA PNG |
| `custom_components/vacuula/brand/icon@2x.png` | 512 × 512 | Transparent, lossless, interlaced RGBA PNG |

The square source serves as both icon and logo. The
[Home Assistant branding guide](https://contributors.home-assistant.io/core/integrations/brands/)
asks for icon files only in this case; Home Assistant provides logo and dark-theme
fallbacks. The original colors are legible on white and dark backgrounds, so
separate dark images are unnecessary. Empty surrounding space is removed, with
only the transparent space needed to keep the artwork undistorted in a square.

Custom integrations bundle these files in `brand/` on Home Assistant 2026.3 and
newer. They take precedence over CDN images without a manifest setting or a
central brands submission. If the integration is later contributed to Core,
the assets must move to the central brands repository.

## Reproduce the PNGs

Run from the repository root with Node.js, npm and ImageMagick 7 installed. The
SVG renderer is a development tool only. Keep its dependencies outside the
repository and the installed integration:

```sh
BRAND_TOOLS=$(mktemp -d)
npm install --prefix "$BRAND_TOOLS" --ignore-scripts --no-audit --no-fund \
  --save-exact @resvg/resvg-js@2.6.2
node - "$BRAND_TOOLS" <<'JS'
const { readFileSync, writeFileSync } = require('node:fs');
const { resolve, join } = require('node:path');
const tools = resolve(process.argv[2]);
const { Resvg } = require(join(tools, 'node_modules/@resvg/resvg-js'));
const svg = readFileSync('custom_components/vacuula/brand/vacuula.svg');
const png = new Resvg(svg, {
  fitTo: { mode: 'width', value: 2048 },
  font: { loadSystemFonts: false },
}).render().asPng();
writeFileSync(join(tools, 'rendered.png'), png);
JS
magick "$BRAND_TOOLS/rendered.png" -trim +repage -background none \
  -gravity center -extent '%[fx:max(w,h)]x%[fx:max(w,h)]' \
  -filter Lanczos -resize 512x512 -strip -define png:compression-level=9 \
  -interlace PNG PNG32:custom_components/vacuula/brand/icon@2x.png
magick custom_components/vacuula/brand/icon@2x.png -filter Lanczos \
  -resize 256x256 -strip -define png:compression-level=9 -interlace PNG \
  PNG32:custom_components/vacuula/brand/icon.png
```

The packaged images were rendered with resvg-js 2.6.2 and ImageMagick 7.1.2.

## Verify the local installation

Restart Home Assistant after initially adding the `brand/` directory so its
integration metadata detects the folder. Authenticated requests to
`/api/brands/integration/vacuula/icon.png?placeholder=no` and
`/api/brands/integration/vacuula/icon@2x.png?placeholder=no` must return HTTP 200
and bytes matching the installed PNGs. Check the `logo` and `dark_` fallback
requests too. `placeholder=no` prevents a generic placeholder from being
mistaken for a successful branding installation.
