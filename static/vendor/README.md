# Vendored browser dependencies

- Konva 10.7.0 — https://konvajs.org/
  - Source: https://registry.npmjs.org/konva/-/konva-10.7.0.tgz
  - Package integrity (SHA-512, base64): `2CwuytBrOlTh7WHCSeXyUeysB2rpwHruUUDD5etYtrzijkCVH+00+CQz5fJUGI0hm7mzxxDLYLw4Wpo+8QGJ4g==`
  - License: MIT; see `konva-LICENSE.txt`.
- Cropper.js 1.6.2 — https://fengyuanchen.github.io/cropperjs/ (`dist/cropper.min.js`, `dist/cropper.min.css`)
  - Source: https://registry.npmjs.org/cropperjs/-/cropperjs-1.6.2.tgz
  - Package integrity (SHA-512, base64): `nhymn9GdnV3CqiEHJVai54TULFAE3VshJTXSqSJKa8yXAKyBKDWdhHarnlIPrshJ0WMFTGuFvG02YjLXfPiuOA==`
  - License: MIT; see `cropper-LICENSE.txt`.

Only the browser distributions are bundled. No CDN or Node.js runtime is required when using the application.

## Ported algorithm

- Flickr justified-layout 4.1.0 — https://github.com/flickr/justified-layout
  - Source: https://registry.npmjs.org/justified-layout/-/justified-layout-4.1.0.tgz
  - Package integrity (SHA-512, base64): `M5FimNMXgiOYerVRGsXZ2YK9YNCaTtwtYp7Hb2308U1Q9TXXHx5G0p08mcVR5O53qf8bWY4NJcPBxE6zuayXSg==`
  - License: MIT; see `justified-layout-LICENSE.txt`.
  - Its row-grouping logic is ported to Python in `justified_layout.py` so the server computes one
    layout that both the Konva preview and the Pillow export use.
