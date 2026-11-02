/* SSH page: the "view public key" dialog.
   CSP is script-src 'self' with no 'unsafe-inline', hence its own file --
   an inline <script> block here silently did nothing. */
  (function() {
    var modal = document.getElementById('modal-pubkey');
    var title = document.getElementById('modal-pubkey-title');
    var content = document.getElementById('modal-pubkey-content');
    var btnCopy = document.getElementById('btn-copy-pubkey');
    var btnClose = document.getElementById('btn-close-modal');

    document.querySelectorAll('.btn-view-pubkey').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var keyId = btn.getAttribute('data-key-id');
        var keyName = btn.getAttribute('data-key-name');
        title.textContent = 'Public Key: ' + keyName;
        content.value = 'Loading...';
        if (modal.showModal) {
          modal.showModal();
        } else {
          modal.setAttribute('open', '');
        }

        fetch('/ssh/' + keyId + '/public-key')
          .then(function(res) { return res.text(); })
          .then(function(text) {
            content.value = text;
          })
          .catch(function(err) {
            content.value = 'Failed to load public key.';
          });
      });
    });

    btnCopy.addEventListener('click', function() {
      if (navigator.clipboard && content.value) {
        navigator.clipboard.writeText(content.value).then(function() {
          btnCopy.textContent = 'Copied!';
          setTimeout(function() { btnCopy.textContent = 'Copy to Clipboard'; }, 2000);
        });
      } else {
        content.select();
        document.execCommand('copy');
        btnCopy.textContent = 'Copied!';
        setTimeout(function() { btnCopy.textContent = 'Copy to Clipboard'; }, 2000);
      }
    });

    btnClose.addEventListener('click', function() {
      if (modal.close) {
        modal.close();
      } else {
        modal.removeAttribute('open');
      }
    });

    modal.addEventListener('click', function(e) {
      if (e.target === modal) {
        if (modal.close) modal.close();
        else modal.removeAttribute('open');
      }
    });
  })();
