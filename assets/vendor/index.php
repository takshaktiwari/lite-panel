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
 */

function adminer_object() {
    class LitePanelAdminer extends Adminer {
        function credentials() {
            // "server" left empty: Adminer connects over the default unix
            // socket, the same one the panel itself uses -- never a TCP host
            // an operator or a forged session could redirect elsewhere.
            return array('server' => '', 'username' => $_POST['auth']['username'] ?? '', 'password' => $_POST['auth']['password'] ?? '');
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
