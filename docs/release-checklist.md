# Release checklist

This page takes you from signing credentials to a published release. It holds no secret
values. Every name below is what `.github/workflows/release.yml` and `scripts/make_dmg.sh`
actually read.

Without any secrets the workflow still builds and smoke-tests everything, and it names the
files `-unsigned`. Gatekeeper and SmartScreen warn on those files.

## 1. Signing credentials (once)

Set each one with `gh secret set <NAME> --repo harvto-llc/ai-usage-tracker`. The command reads
the value from stdin, so the value never lands in a file or in shell history.

### macOS: five secrets

| Secret | What it is | How to produce it |
|--------|------------|-------------------|
| `MACOS_CERT_P12` | Your **Developer ID Application** certificate **and its private key**, as a base64 `.p12` | In Keychain Access, open the **login** keychain, then **My Certificates**. Expand "Developer ID Application: …" so the private key shows under it. Select the certificate row and choose Export. Save as `.p12` with a password. Then run `base64 -i DeveloperID.p12 \| gh secret set MACOS_CERT_P12 --repo harvto-llc/ai-usage-tracker`. Delete the `.p12` afterwards. |
| `MACOS_CERT_PASSWORD` | The password you set on that `.p12` export | Type it at the `gh secret set` prompt |
| `NOTARY_APPLE_ID` | The Apple ID email of the developer account | From your account |
| `NOTARY_TEAM_ID` | The 10-character Team ID | developer.apple.com → Account → Membership details |
| `NOTARY_PASSWORD` | An **app-specific password** for notarytool | appleid.apple.com → Sign-In and Security → App-Specific Passwords → generate one named "ai-cur notary" |

The certificate type matters. The job fails with "MACOS_CERT_P12 holds no Developer ID
Application identity" if the `.p12` holds any other kind: Apple Development, Mac App
Distribution, or Developer ID **Installer**.

**What first-time releasers get wrong on macOS:**

- **The `.p12` needs the private key.** A certificate exported on its own, or one downloaded
  from developer.apple.com, has no key. `codesign` then finds no usable identity. Export from
  the login keychain with the key visible under the certificate, as described above.
- **notarytool needs an app-specific password, not your Apple ID password.** Your normal
  password is rejected, even with two-factor authentication on.

All five must be set for notarization. The certificate pair alone gives a Developer ID signed
dmg that is **not notarized**, and Gatekeeper still warns on first open.

### Windows: two secrets

| Secret | What it is | How to produce it |
|--------|------------|-------------------|
| `WINDOWS_CERT_PFX` | An Authenticode code-signing certificate **with its private key**, as a base64 `.pfx` | Export from the Windows certificate store with "Yes, export the private key" checked, or get the `.pfx` from your certificate authority. Then run `base64 -i codesign.pfx \| gh secret set WINDOWS_CERT_PFX --repo harvto-llc/ai-usage-tracker`. |
| `WINDOWS_CERT_PASSWORD` | The `.pfx` password | Type it at the `gh secret set` prompt |

The workflow signs every `.exe` it froze and then the installer. It uses `signtool` with a
SHA-256 digest and the DigiCert timestamp server.

**What first-time releasers get wrong on Windows: certificates on a hardware token.** Since
June 2023, OV and EV code-signing certificates are issued on a hardware token or an HSM. Their
private key **cannot be exported to a `.pfx`**, so this `.pfx` path only works with an older
exportable certificate. With a token-based certificate you need cloud signing instead, for
example Azure Trusted Signing (`azure/trusted-signing-action`) or your CA's cloud HSM, called
from the windows job in place of the signtool steps. **That path is not built.** Today the
workflow has only the `.pfx` path. Until someone builds the cloud path, a token-only
certificate means an unsigned Windows installer.

### Linux

No signing. Before the first public release, replace the `.deb` maintainer placeholder
`maintainer@harvto.invalid` in `scripts/build_linux.sh` with a real address.

## 2. Cut the release

1. Merge the release branch into `main` through its pull request. The `gate` check must be
   green.
2. Pick a version `X.Y.Z` that has never been used. Tags are never reused, not even after a
   yank.
3. Tag `main`'s merge commit and push the tag:

   ```bash
   git fetch origin
   git tag -a vX.Y.Z origin/main -m "ai-cur desktop client X.Y.Z"
   git push origin vX.Y.Z
   ```

4. The `release` workflow runs on the tag. The build jobs are macos, windows and linux. The
   `gate` job must succeed. Then the `release` job creates a **draft** release named
   "ai-cur desktop client vX.Y.Z" with all artifacts and their `.sha256` files attached.
   Nothing is published automatically.

## 3. Check the draft before publishing

Download every file from the draft release into an empty folder. Then:

**Smoke logs.** Open the workflow run for the tag. For each job:
- the "Negative control" step must say `negative control failed as required (exit 3)`
- every "Smoke test" step must end in `SMOKE PASS`
- on Windows, the log must contain `dummy Claude session pid … still alive after refresh`

**Checksums.** Run `shasum -a 256 -c <file>.sha256` for each file. On Windows use
`Get-FileHash` and compare the value by eye.

**File names.** With secrets set, the files carry **no** `-unsigned` suffix. If the suffix is
there, the secrets were not read, so stop.

**macOS dmg** (on a Mac other than the build machine if you can):

```bash
xcrun stapler validate ai-cur-desktop-X.Y.Z.dmg          # "The validate action worked!"
spctl -a -vv -t open --context context:primary-signature ai-cur-desktop-X.Y.Z.dmg
                                                          # "accepted", source=Notarized Developer ID
hdiutil attach -nobrowse ai-cur-desktop-X.Y.Z.dmg
spctl -a -vv "/Volumes/ai-cur desktop client/Usage Tracker.app"
                                                          # "accepted", source=Notarized Developer ID
codesign --verify --deep --strict --verbose=2 "/Volumes/ai-cur desktop client/Usage Tracker.app"
hdiutil detach "/Volumes/ai-cur desktop client"
```

Then install it for real: drag the app to Applications and open it. There must be no
Gatekeeper prompt beyond the usual "downloaded from the internet" confirmation. The menu bar
icon should show numbers within a minute.

**Windows installer** (Developer PowerShell, or any shell with `signtool` on PATH):

```powershell
signtool verify /pa /v ai-cur-desktop-setup-X.Y.Z.exe      # "Successfully verified"
```

Then run the installer and check the installed files:

```powershell
signtool verify /pa "$env:LOCALAPPDATA\Programs\ai-cur desktop client\aicur-tray.exe"
signtool verify /pa "$env:LOCALAPPDATA\Programs\ai-cur desktop client\backend\aicur-backend.exe"
```

SmartScreen can still warn for a new certificate until it builds reputation. That warning is
not a signing failure.

**Linux.** `sudo apt install ./ai-cur-desktop_X.Y.Z_amd64.deb` on Ubuntu 22.04 or later. The
tray icon appears where a tray host exists. On stock GNOME the one-time AppIndicator message
appears instead. Then run `chmod +x` on the AppImage and run it once.

**Homebrew cask.** CI does not upload the rendered cask. Render it from the template with the
published dmg's checksum (the same substitution `make_dmg.sh` does), then submit it to the tap:

```bash
V=X.Y.Z; SHA=$(cut -d' ' -f1 ai-cur-desktop-$V.dmg.sha256)
sed -e "s/__VERSION__/$V/g" -e "s/__SHA256__/$SHA/g" \
    -e "s|__URL__|https://github.com/harvto-llc/ai-usage-tracker/releases/download/v$V/ai-cur-desktop-$V.dmg|g" \
    packaging/homebrew/usage-tracker.rb.template > ai-cur-desktop.rb
ruby -c ai-cur-desktop.rb
```

Only when all of the above passes: edit the draft release, write the notes (start from
`CHANGELOG.md`, and move "Unreleased" under the new version in a follow-up PR), and click
**Publish release**.

## 4. Yank a bad release

The app's Settings checks GitHub Releases for the newest stable version. A release that stays
published keeps being offered.

1. Stop new downloads first. Either turn it back into a draft:

   ```bash
   gh release edit vX.Y.Z --draft --repo harvto-llc/ai-usage-tracker
   ```

   or, if its files must never be fetched again, delete it along with its tag:

   ```bash
   gh release delete vX.Y.Z --cleanup-tag --yes --repo harvto-llc/ai-usage-tracker
   ```

2. If the Homebrew cask already points at the bad version, revert the cask in the tap to the
   previous version.
3. Fix forward. Release the fix as `vX.Y.(Z+1)` through this same checklist. Never re-tag or
   re-upload under the yanked version: people and caches already hold its checksum.
4. Add a line to `CHANGELOG.md` under the new version saying what was yanked and why.
5. If the bad build could harm installed machines (for example a backend that kills
   processes, or data written outside `~/.usage-tracker`), say so in the new release notes and
   tell users how to uninstall:
   - macOS: quit the app, then delete it
   - Windows: Settings → Apps → ai-cur desktop client → Uninstall
   - Linux: `sudo apt remove ai-cur-desktop`, or `<AppImage> --uninstall`

   Their data in `~/.usage-tracker` is left in place.
