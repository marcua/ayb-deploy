# Deployment wrapper for the public ayb image
# This just pulls the latest public image and lets Kamal add its required labels
FROM ghcr.io/marcua/ayb:latest

# No changes needed - Kamal will add its service labels during build
