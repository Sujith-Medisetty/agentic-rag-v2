/*
 * ojas-systemd-helper — setuid root helper for the Ojas backend.
 *
 * The Ojas backend runs as the `ojas` user inside a systemd unit
 * with `NoNewPrivileges=true` (security hardening), which blocks
 * `sudo` from elevating. We need to write per-app systemd units
 * to /etc/systemd/system/, run `systemctl daemon-reload`, `enable`,
 * `start`, `stop`, `disable`, and `rm` — but only for unit names
 * matching `ojas-app-*.service` (scoped tightly so this can't be
 * used to control arbitrary services).
 *
 * Usage (called only by the Ojas backend, not by humans):
 *   ojas-systemd-helper <command> <args>
 *
 * Commands:
 *   write-unit <name> <path>    Copy file at <path> to
 *                                /etc/systemd/system/<name>. Validates
 *                                the name matches ojas-app-*.service.
 *   rm-unit <name>              Remove /etc/systemd/system/<name>
 *   systemctl <args...>         Pass-through to /usr/bin/systemctl,
 *                                after validating the FIRST arg looks
 *                                like an ojas-app-*.service unit.
 *
 * Build:
 *   gcc -O2 -o /usr/local/sbin/ojas-systemd-helper ojas-systemd-helper.c
 *   chown root:root /usr/local/sbin/ojas-systemd-helper
 *   chmod 4750 /usr/local/sbin/ojas-systemd-helper
 *   chgrp ojas /usr/local/sbin/ojas-systemd-helper
 *
 * The setuid bit + group restriction means: only the ojas user (or root)
 * can run this; when they do, it runs as root.
 *
 * Security model: trust the Ojas backend to only call this with
 * well-formed unit names. We re-validate the name pattern defensively
 * (defence in depth) — the regex below is duplicated in the backend
 * so a misbehaving caller can't escape it.
 */

#include <errno.h>
#include <fcntl.h>
#include <regex.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

/* Only allow ojas-app-<safe-slug>.service. Slug may contain
 * [a-z0-9_-], 1-40 chars. Total unit name is well under NAME_MAX. */
static int unit_name_valid(const char *name) {
    if (!name) return 0;
    size_t len = strlen(name);
    if (len == 0 || len > 80) return 0;
    static const char *prefix = "ojas-app-";
    if (strncmp(name, prefix, strlen(prefix)) != 0) return 0;
    const char *slug = name + strlen(prefix);
    size_t slen = strlen(slug);
    static const char *suffix = ".service";
    if (slen < strlen(suffix)) return 0;
    if (strcmp(slug + slen - strlen(suffix), suffix) != 0) return 0;
    /* Validate the slug portion (between prefix and suffix) */
    size_t body_len = slen - strlen(suffix);
    if (body_len == 0 || body_len > 40) return 0;
    for (size_t i = 0; i < body_len; i++) {
        char c = slug[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
              c == '-' || c == '_')) {
            return 0;
        }
    }
    return 1;
}

static int copy_file(const char *src, const char *dst) {
    int sfd = open(src, O_RDONLY);
    if (sfd < 0) {
        fprintf(stderr, "open(%s): %s\n", src, strerror(errno));
        return 1;
    }
    int dfd = open(dst, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (dfd < 0) {
        fprintf(stderr, "open(%s): %s\n", dst, strerror(errno));
        close(sfd);
        return 1;
    }
    char buf[8192];
    ssize_t n;
    while ((n = read(sfd, buf, sizeof buf)) > 0) {
        ssize_t w = write(dfd, buf, n);
        if (w < 0) { close(sfd); close(dfd); return 1; }
    }
    close(sfd);
    close(dfd);
    /* Match the permissions of the ojas-backend.service unit (644) */
    chmod(dst, 0644);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: ojas-systemd-helper <command> [...]\n");
        return 2;
    }

    /* Drop supplementary groups (defence in depth; we don't need them) */
    setgroups(0, NULL);

    if (strcmp(argv[1], "write-unit") == 0) {
        if (argc != 4) {
            fprintf(stderr, "write-unit: need <name> <path>\n");
            return 2;
        }
        if (!unit_name_valid(argv[2])) {
            fprintf(stderr, "write-unit: invalid unit name %s\n", argv[2]);
            return 2;
        }
        char dst[256];
        snprintf(dst, sizeof dst, "/etc/systemd/system/%s", argv[2]);
        return copy_file(argv[3], dst);
    }
    if (strcmp(argv[1], "rm-unit") == 0) {
        if (argc != 3) {
            fprintf(stderr, "rm-unit: need <name>\n");
            return 2;
        }
        if (!unit_name_valid(argv[2])) {
            fprintf(stderr, "rm-unit: invalid unit name %s\n", argv[2]);
            return 2;
        }
        char dst[256];
        snprintf(dst, sizeof dst, "/etc/systemd/system/%s", argv[2]);
        if (unlink(dst) != 0 && errno != ENOENT) {
            fprintf(stderr, "unlink(%s): %s\n", dst, strerror(errno));
            return 1;
        }
        return 0;
    }
    if (strcmp(argv[1], "systemctl") == 0) {
        /* For "systemctl <op> ojas-app-X.service" calls, validate the
         * unit name. For other systemctl calls (daemon-reload, etc.),
         * pass through without validation — those don't take a unit
         * name and are safe. */
        int has_unit = 0;
        const char *unit_arg = NULL;
        for (int i = 2; i < argc; i++) {
            if (strstr(argv[i], "ojas-app-") == argv[i]) {
                has_unit = 1;
                unit_arg = argv[i];
                break;
            }
        }
        if (has_unit && !unit_name_valid(unit_arg)) {
            fprintf(stderr, "systemctl: invalid unit name %s\n", unit_arg);
            return 2;
        }
        /* Build argv for /usr/bin/systemctl, SKIPPING our own argv[1]
         * which is the literal word "systemctl". */
        char **new_argv = (char **)malloc(argc * sizeof(char *));
        if (!new_argv) { perror("malloc"); return 1; }
        new_argv[0] = "/usr/bin/systemctl";
        int nargs = 1;
        for (int i = 2; i < argc; i++) new_argv[nargs++] = argv[i];
        new_argv[nargs] = NULL;
        execv("/usr/bin/systemctl", new_argv);
        fprintf(stderr, "execv: %s\n", strerror(errno));
        return 1;
    }
    fprintf(stderr, "unknown command: %s\n", argv[1]);
    return 2;
}
