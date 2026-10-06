/* Host checks include the original renderer to inspect its private mouth rasterizer. */
#include "../public/esp32-avatar/source/muse_pixel.c"
#include <assert.h>
#include <stdio.h>

int main(int argc, char **argv) {
    if (argc != 4) return 2;
    const int size = atoi(argv[1]), frame = atoi(argv[2]);
    const float level = strtof(argv[3], NULL);
    /* The upstream 0..0.1 idle mouth perturbation quantizes to a single closed line. */
    for (int i = 0; i < 160; i++) {
        memset(s_fb, 0, sizeof(s_fb));
        const float open = 0.1f * (0.5f + 0.5f * sinf(i / 20.0f * 22.0f));
        draw_mouth(32, 32, MOUTH_TALK, open);
        int rows = 0;
        for (int y = 0; y < 64; y++) {
            bool used = false;
            for (int x = 0; x < 64; x++) if (s_fb[y * 64 + x]) used = true;
            rows += used;
        }
        assert(rows == 1);
    }
    for (int i = 0; i <= frame; i++) {
        muse_pose_t pose = {.mode=MUSE_MODE_SPEAKING, .t=i/20.0f, .mode_t=i/20.0f, .level=level};
        muse_pixel_render(&pose);
    }
    uint16_t *buffer = malloc(size * size * sizeof(uint16_t));
    assert(buffer);
    muse_pixel_set_size(size);
    muse_pixel_scale(buffer, size, 0, size - 1, 0, size - 1);
    for (int i = 0; i < size * size; i++) {
        const uint16_t px = buffer[i];
        const unsigned char bytes[] = {((px>>11)&31)*255/31, ((px>>5)&63)*255/63, (px&31)*255/31};
        fwrite(bytes, 1, 3, stdout);
    }
    free(buffer);
    return ferror(stdout) ? 1 : 0;
}
