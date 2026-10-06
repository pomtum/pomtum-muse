/* Host-only adapter. The original Meta renderer is compiled separately unchanged. */
#include "muse_pixel.h"
#include <stdio.h>
#include <stdlib.h>

static void rgb(uint16_t px) {
    const unsigned char bytes[3] = {
        (unsigned char)(((px >> 11) & 31) * 255 / 31),
        (unsigned char)(((px >> 5) & 63) * 255 / 63),
        (unsigned char)((px & 31) * 255 / 31),
    };
    fwrite(bytes, 1, 3, stdout);
}

int main(int argc, char **argv) {
    if (argc != 6) return 2;
    const int mode = atoi(argv[1]), frames = atoi(argv[3]);
    const float level = strtof(argv[2], NULL), fps = strtof(argv[4], NULL);
    const int pet = atoi(argv[5]);
    if (mode < 0 || mode >= MUSE_MODE_COUNT || frames < 1 || fps <= 0) return 2;
    uint16_t full[64 * 64], dim_row[192];
    for (int frame = 0; frame < frames; frame++) {
        const float t = frame / fps;
        const muse_pose_t pose = { .mode = mode, .t = t, .mode_t = t,
            .level = level, .happy = pet ? 1.0f - (float)frame / frames : 0 };
        muse_pixel_render(&pose);
        muse_pixel_set_size(64);
        muse_pixel_scale(full, 64, 0, 63, 0, 63);
        /* Sample the renderer's own dim palette at its 3x grid edge. */
        muse_pixel_set_size(192);
        for (int y = 0; y < 64; y++) {
            muse_pixel_scale(dim_row, 192, 0, 191, y * 3 + 2, y * 3 + 2);
            for (int x = 0; x < 64; x++) rgb(full[y * 64 + x]);
            for (int x = 0; x < 64; x++) rgb(dim_row[x * 3]);
        }
    }
    return ferror(stdout) ? 1 : 0;
}
