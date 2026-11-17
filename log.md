$ runuser -u site_ai_eoschambersoflaw_com -- env HOME=/var/www/ai-eoschambersoflaw_com GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND=ssh -i /var/www/ai-eoschambersoflaw_com/.ssh/lite_panel_deploy -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/var/www/ai-eoschambersoflaw_com/.ssh/known_hosts git -C /var/www/ai-eoschambersoflaw_com remote set-url origin git@github.com:takshaktiwari/manage_eos.git
Fetching main from git@github.com:takshaktiwari/manage_eos.git
$ runuser -u site_ai_eoschambersoflaw_com -- env HOME=/var/www/ai-eoschambersoflaw_com GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND=ssh -i /var/www/ai-eoschambersoflaw_com/.ssh/lite_panel_deploy -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/var/www/ai-eoschambersoflaw_com/.ssh/known_hosts git -C /var/www/ai-eoschambersoflaw_com fetch --prune origin +refs/heads/main:refs/remotes/origin/main
From github.com:takshaktiwari/manage_eos
   a5d60dc..975393c  main       -> origin/main
$ runuser -u site_ai_eoschambersoflaw_com -- env HOME=/var/www/ai-eoschambersoflaw_com GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND=ssh -i /var/www/ai-eoschambersoflaw_com/.ssh/lite_panel_deploy -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/var/www/ai-eoschambersoflaw_com/.ssh/known_hosts git -C /var/www/ai-eoschambersoflaw_com checkout -q -f -B main origin/main
Now at 975393c feat: add local AI prompt gate and service for classification and name finding
Running the deploy script (each command is shown with a + before it):
$ runuser -u site_ai_eoschambersoflaw_com -- env HOME=/var/www/ai-eoschambersoflaw_com GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND=ssh -i /var/www/ai-eoschambersoflaw_com/.ssh/lite_panel_deploy -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/var/www/ai-eoschambersoflaw_com/.ssh/known_hosts PATH=/usr/local/bin:/usr/bin:/bin bash -e -o pipefail -x /var/www/ai-eoschambersoflaw_com/tmp/deploy-1794888143-51964.sh
+ composer install --no-dev --optimize-autoloader --no-interaction
Installing dependencies from lock file
Verifying lock file contents can be installed on current platform.
Package operations: 0 installs, 0 updates, 33 removals
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing theseer/tokenizer (2.0.1)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing staabm/side-effects-detector (1.0.5)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing sebastian/version (6.0.1)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing sebastian/type (6.0.4)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing sebastian/recursion-context (7.0.1)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing sebastian/object-reflector (5.0.0)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing sebastian/object-enumerator (7.0.0)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing sebastian/lines-of-code (4.0.1)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing sebastian/global-state (8.0.3)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing sebastian/exporter (7.0.3)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing sebastian/environment (8.1.2)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing sebastian/diff (7.0.1)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing sebastian/complexity (5.0.0)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing sebastian/comparator (7.1.8)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing sebastian/cli-parser (4.2.1)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing phpunit/phpunit (12.5.38)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing phpunit/php-timer (8.0.0)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing phpunit/php-text-template (5.0.1)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing phpunit/php-invoker (6.0.0)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing phpunit/php-file-iterator (6.0.2)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing phpunit/php-code-coverage (12.5.8)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing phar-io/version (3.2.1)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing phar-io/manifest (2.0.4)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing nunomaduro/collision (v8.9.5)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing myclabs/deep-copy (1.14.0)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing mockery/mockery (1.6.15)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing laravel/pint (v1.32.1)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing laravel/pao (v1.1.5)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing laravel/pail (v1.2.7)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing laravel/agent-detector (v2.0.4)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing hamcrest/hamcrest-php (v3.0.0)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing filp/whoops (2.18.6)
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/staabm/side-effects-detector/lib/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/type/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-enumerator/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/global-state/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/theseer/tokenizer/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/lines-of-code/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/version/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/exporter/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/object-reflector/src/" which does not appear to be a file nor a folder
Could not scan for classes inside "/var/www/ai-eoschambersoflaw_com/vendor/sebastian/recursion-context/src/" which does not appear to be a file nor a folder
> Illuminate\Foundation\ComposerScripts::prePackageUninstall
  - Removing fakerphp/faker (v1.24.1)
  0/23 [>---------------------------]   0%
 10/23 [============>---------------]  43%
 23/23 [============================] 100%
Generating optimized autoload files
> Illuminate\Foundation\ComposerScripts::postAutoloadDump
> @php artisan package:discover --ansi

  [37;44m INFO [39;49m Discovering packages.  

  laravel/ai [90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m [32;1mDONE[39;22m
  laravel/fortify [90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m [32;1mDONE[39;22m
  laravel/passkeys [90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m [32;1mDONE[39;22m
  laravel/tinker [90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m [32;1mDONE[39;22m
  nesbot/carbon [90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m [32;1mDONE[39;22m
  nunomaduro/termwind [90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m [32;1mDONE[39;22m
  spatie/laravel-permission [90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m[90m.[39m [32;1mDONE[39;22m

67 packages you are using are looking for funding.
Use the `composer fund` command to find out more!
+ npm install

up to date, audited 97 packages in 640ms

47 packages are looking for funding
  run `npm fund` for details

found 0 vulnerabilities
+ npm run build
