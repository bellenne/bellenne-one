"""Non-user-facing Folio safety limits derived from the Ozon contract."""

# Ozon's send-message contract accepts plain text from 1 to 1000 characters.
MESSAGE_MAX_CHARS = 1000

# Keep downloaded and uploaded images bounded even when the remote response
# omits Content-Length. This is an internal safety ceiling, not a product
# setting an administrator should have to understand.
IMAGE_MAX_BYTES = 10 * 1024 * 1024

# Ozon currently serves marketplace media from its own DNS namespaces. Every
# resolved address is still checked for global routability before downloading.
OZON_MEDIA_ROOTS = ("ozon.ru", "ozone.ru")

# A photo is printed as is unless the part left after fitting it to the canvas
# falls short of the print size (150 DPI) by this many pixels on either side.
GENERATION_SHORTFALL_PX = 1500

# Links from buyers lead anywhere, so one download stays bounded in time and hops.
LINK_MAX_REDIRECTS = 5
LINK_DEADLINE_SECONDS = 30
