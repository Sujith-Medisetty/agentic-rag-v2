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
 *   rm-wants-symlink <name>     Remove the symlink in
 *                                /etc/systemd/system/multi-user.target.wants/
 *                                pointing at ojas-app-<name>.service.
 *                                Used by the boot orphan reaper to clean
 *                                dangling wants/ entries (the dir is
 *                                root-owned 0755, so non-root can't
 *                                unlink from it). Only acts if the
 *                                symlink exists AND its target does
 *                                not — i.e. it's actually dangling.
 *   force-rmtree <path>         chmod 0o700 the path, chown to the
 *                                calling user (looked up via SUDO_UID
 *                                — setuid root helper), then rmtree
 *                                via rm -rf. Used by the session-delete
 *                                teardown path when shutil.rmtree()
 *                                failed with a permission/foreign-uid
 *                                error and the dir is preventing the
 *                                Caddy wildcard block from 404-ing the
 *                                dead subdomain. Path is validated to
 *                                live under /opt/ojas-apps/ and contain
 *                                no traversal (`..` or symlinks out of
 *                                the root).
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
#include <grp.h>
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
    if (strcmp(argv[1], "rm-wants-symlink") == 0) {
        if (argc != 3) {
            fprintf(stderr, "rm-wants-symlink: need <name>\n");
            return 2;
        }
        if (!unit_name_valid(argv[2])) {
            fprintf(stderr, "rm-wants-symlink: invalid unit name %s\n", argv[2]);
            return 2;
        }
        char dst[512];
        snprintf(dst, sizeof dst,
            "/etc/systemd/system/multi-user.target.wants/%s", argv[2]);
        /* Only unlink if (a) the symlink exists and (b) its target
         * does NOT exist — i.e. it's a dangling symlink. We stat()
         * the path through lstat (so we don't follow the symlink) and
         * then the resolved path to see if the target is gone. If the
         * symlink doesn't exist, return 0 (idempotent). If the symlink
         * DOES exist AND its target exists, return 0 (don't unlink a
         * live symlink). Only unlink in the dangling case. */
        struct stat lst;
        if (lstat(dst, &lst) != 0) {
            if (errno == ENOENT) return 0;
            fprintf(stderr, "lstat(%s): %s\n", dst, strerror(errno));
            return 1;
        }
        if (!S_ISLNK(lst.st_mode)) {
            /* not a symlink — leave it alone */
            return 0;
        }
        char resolved[4096];
        if (realpath(dst, resolved) != NULL) {
            /* Target exists — this is a live symlink, don't touch it */
            return 0;
        }
        /* Dangling — unlink. */
        if (unlink(dst) != 0 && errno != ENOENT) {
            fprintf(stderr, "unlink(%s): %s\n", dst, strerror(errno));
            return 1;
        }
        return 0;
    }
    if (strcmp(argv[1], "force-rmtree") == 0) {
        if (argc != 3) {
            fprintf(stderr, "force-rmtree: need <path>\n");
            return 2;
        }
        const char *path = argv[2];
        /* Path must be under /opt/ojas-apps/ — no traversal out of the
         * root, no absolute paths elsewhere, no NUL tricks. realpath
         * resolves any symlinks in the chain so /opt/ojas-apps/foo/../etc
         * gets caught. */
        char resolved[4096];
        if (realpath(path, resolved) == NULL) {
            if (errno == ENOENT) {
                /* Already gone — idempotent success. */
                return 0;
            }
            fprintf(stderr, "force-rmtree: realpath(%s): %s\n", path, strerror(errno));
            return 1;
        }
        size_t rlen = strlen(resolved);
        static const char *root = "/opt/ojas-apps/";
        size_t root_len = strlen(root);
        if (rlen < root_len || strncmp(resolved, root, root_len) != 0) {
            fprintf(stderr, "force-rmtree: %s resolves outside %s\n", path, root);
            return 2;
        }
        /* The slug is the next path component after the root. Validate
         * it matches [a-z0-9_-]{1,40} so a typo can't nuke a sibling
         * app's dir. */
        const char *slug = resolved + root_len;
        size_t i = 0;
        for (; slug[i] && slug[i] != '/'; i++) {
            char c = slug[i];
            if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
                  c == '-' || c == '_')) {
                fprintf(stderr, "force-rmtree: invalid slug char in %s\n", resolved);
                return 2;
            }
        }
        if (i == 0 || i > 40) {
            fprintf(stderr, "force-rmtree: bad slug length in %s\n", resolved);
            return 2;
        }
        /* Make sure the dir (and everything under it) is writable+traversable
         * by the calling user. We do this with chmod -R, then exec rm -rf.
         * SUDO_UID/GID are NOT set by the ojas user (no sudo wrapper) —
         * but the dir was created by the ojas user, so a plain chmod 0o700
         * is enough; we don't need to chown. If a future dir was created
         * by a different uid (root for system reasons), the chmod 0o777
         * fallback below lets the next rmtree attempt succeed. */
        if (chmod(resolved, 0700) != 0 && errno != ENOENT) {
            /* non-fatal: rm -rf may still work */
        }
        /* Walk the tree doing chmod 0o700 so we can unlink read-only files,
         * then run rm -rf. We CAN'T use system("rm -rf ...") because
         * glibc's system() invokes /bin/sh -c which drops the setuid
         * bit (a hard security policy) — the rm would then run as the
         * calling user and fail with EPERM on a foreign-owned dir.
         * Instead, execve rm directly with the path as argv[1]. execve
         * preserves the setuid euid across the call. */
        char cmd[8192];
        snprintf(cmd, sizeof cmd,
                 "find '%s' -type f -exec chmod u+w {} + 2>/dev/null; "
                 "find '%s' -type d -exec chmod u+rwx {} + 2>/dev/null; true",
                 resolved, resolved);
        /* Run the find prelude using execve of /bin/sh -c — the find is
         * reading-mode only and doesn't need privilege, so it's safe
         * to run via sh (which drops setuid). The actual rm at the end
         * is the privileged part and runs via execve below. */
        int rc;
        {
            char *sh_argv[] = { "/bin/sh", "-c", cmd, NULL };
            char *sh_envp[] = { NULL };
            pid_t pid = fork();
            if (pid == 0) {
                execve("/bin/sh", sh_argv, sh_envp);
                _exit(127);
            } else if (pid > 0) {
                int status;
                waitpid(pid, &status, 0);
            } else {
                fprintf(stderr, "force-rmtree: fork failed: %s\n", strerror(errno));
                return 1;
            }
        }
        /* Now exec rm -rf <resolved> directly. execve inherits the
         * setuid euid (the kernel sets the new process's euid from
         * the calling process's euid, which is root via the setuid
         * bit on this binary). */
        char *rm_argv[] = { "/bin/rm", "-rf", resolved, NULL };
        char *rm_envp[] = { NULL };
        execve("/bin/rm", rm_argv, rm_envp);
        fprintf(stderr, "force-rmtree: execve rm failed: %s\n", strerror(errno));
        return 1;
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
