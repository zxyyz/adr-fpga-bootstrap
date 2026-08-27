# Security policy

## Never publish secrets

Do not commit or attach any of the following:

- AMD account credentials or `wi_authentication_key` files.
- `.lic` license files or FlexNet private material.
- SSH private keys, access tokens, passwords, or hardware credentials.
- Proprietary AMD installers or installed Vivado/Vitis trees.
- Private FPGA projects, bitstreams, XSA files, captures, or board identities.

The supplied wrappers mount licenses read-only from a private host directory.
Installation logs and generated artifacts should remain outside this source
repository.

Report a suspected vulnerability through GitHub's private security advisory
feature instead of opening a public issue containing sensitive details.

