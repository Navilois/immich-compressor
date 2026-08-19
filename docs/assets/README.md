# Assets

## The social preview

`social-preview.png` is the image GitHub shows when the repository is linked on Mastodon,
Reddit, Discord or in a chat. **It has to be uploaded by hand** — GitHub does not read it
from the repository:

**Settings → General → Social preview → Upload an image**

Without it a shared link renders as a grey placeholder with the owner's avatar, which is the
difference between a link people click and one they scroll past.

`social-preview.svg` is the source; the PNG is committed so it can be uploaded without
rendering anything. To regenerate it after editing the SVG (1280×640 is what GitHub wants):

```bash
docker run --rm -v "$PWD:/w" -w /w alpine sh -c \
  'apk add --no-cache rsvg-convert ttf-dejavu >/dev/null &&
   rsvg-convert -w 1280 -h 640 docs/assets/social-preview.svg -o docs/assets/social-preview.png'
```

The project's own image cannot do this: ImageMagick is installed without an SVG delegate, on
purpose — nothing in the pipeline rasterises vectors.

Keep the text short enough to read at thumbnail size. This is seen at about 400 px wide far
more often than at full size.
