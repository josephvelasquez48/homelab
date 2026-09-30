#!/bin/sh
# Close the homelab display so the Pi's desktop shows. Run by the
# homelab-desktop:// link the display's Desktop button follows; the link
# arrives as $1 and isn't needed. The "Homelab display" icon brings it back.
exec systemctl --user --no-block stop pi-display.service
