<?php
/**
 * lite-panel's Adminer entry point.
 *
 * Loaded instead of adminer.php directly so the connection target can be
 * pinned to this machine's own MariaDB. Without this, Adminer's login form
 * lets a visitor type any hostname into the "Server" field -- reachable only
 * behind the panel's session gate, but a forged/stolen session could then
 * point it at an arbitrary internal or external host. Overriding
 * credentials() removes that field from the form entirely and ignores
 * whatever is submitted, so this Adminer can only ever reach the database it
 * was installed to manage.
 *
 * adminer.php itself is untouched, vendored upstream — this file only adds
 * the override on top, so upgrading Adminer is a matter of replacing that
 * one file.
 *
 * credentials() must return a plain numerically-indexed array (server,
 * username, password) -- every caller in adminer.php reads it as $arr[0],
 * $arr[1], $arr[2], never by key. An earlier version of this override
 * returned an associative array instead, which silently broke every page
 * load *after* the login redirect: the login POST itself never calls
 * credentials() (it stores the password and redirects immediately), but
 * every subsequent GET reconnects by calling it, and $arr[1]/$arr[2] on an
 * associative array are simply null -- indistinguishable from "not logged
 * in" to mysqli. Login appeared to work (the redirect happened) while
 * actually browsing anything failed with "Access denied for user ''@...".
 * Also: the username/password for that reconnect must come from
 * $_GET["username"] and Adminer's own get_password() (which decrypts what
 * the login POST stored in $_SESSION, via the adminer_key cookie) -- not
 * from $_POST, which is empty on a GET.
 */

function adminer_object() {
    class LitePanelAdminer extends Adminer {
        function credentials() {
            // "server" left empty: Adminer connects over the default unix
            // socket, the same one the panel itself uses -- never a TCP host
            // an operator or a forged session could redirect elsewhere.
            return array(
                '',
                $_POST['auth']['username'] ?? $_GET['username'] ?? '',
                $_POST['auth']['password'] ?? get_password(),
            );
        }

        function permanentLogin($create = false) {
            // No "remember me" cookie: a stolen panel session should not
            // also leave a standing database credential behind.
            return '';
        }
    }

    return new LitePanelAdminer;
}

include __DIR__ . '/adminer.php';
