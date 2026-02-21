#!/bin/bash
# Generate all required icon sizes from icon.png using macOS sips.
# Usage: cd icons && ./generate.sh

set -e

SRC="icon.png"

if [ ! -f "$SRC" ]; then
    echo "Error: $SRC not found in the current directory."
    exit 1
fi

SIZES=(72 96 128 144 152 192 384 512)
for size in "${SIZES[@]}"; do
    cp "$SRC" "icon-${size}x${size}.png"
    sips -z $size $size "icon-${size}x${size}.png" --out "icon-${size}x${size}.png" > /dev/null 2>&1
    echo "  icon-${size}x${size}.png"
done

# Apple Touch Icon (180x180)
cp "$SRC" "apple-touch-icon.png"
sips -z 180 180 "apple-touch-icon.png" --out "apple-touch-icon.png" > /dev/null 2>&1
echo "  apple-touch-icon.png"

# Favicons
for size in 16 32; do
    cp "$SRC" "favicon-${size}x${size}.png"
    sips -z $size $size "favicon-${size}x${size}.png" --out "favicon-${size}x${size}.png" > /dev/null 2>&1
    echo "  favicon-${size}x${size}.png"
done

echo "Done. All icons generated from $SRC."
