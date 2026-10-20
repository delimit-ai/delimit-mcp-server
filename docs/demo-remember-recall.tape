# Source for docs/demo-remember-recall.gif (README "Quick start").
#
# Records the real CLI. Nothing is mocked: `npx delimit-cli` runs the
# package built from this repository, installed into a throwaway project,
# with a throwaway HOME so no real memories, paths or usernames appear.
#
# Regenerate (needs vhs >= 0.10, ttyd, ffmpeg, chromium):
#
#   npm pack --pack-destination /tmp/demo-pack
#   mkdir -p /tmp/demo-proj && cd /tmp/demo-proj
#   echo '{"name":"demo","private":true}' > package.json
#   npm install /tmp/demo-pack/delimit-cli-*.tgz
#   cd - && DEMO_PROJ=/tmp/demo-proj vhs docs/demo-remember-recall.tape

Output docs/demo-remember-recall.gif

Set Shell "bash"
Set FontFamily "DejaVu Sans Mono"
Set FontSize 14
Set Width 1000
Set Height 300
Set Padding 10
Set Theme "Dracula"
Set TypingSpeed 35ms
Set Framerate 12
Set PlaybackSpeed 1.0

# Hidden setup: fresh empty HOME, neutral prompt, move into the throwaway project.
Hide
Type `export HOME=$(mktemp -d) npm_config_update_notifier=false PS1='$ ' && unset PROMPT_COMMAND && cd "$DEMO_PROJ" && clear`
Enter
Sleep 500ms
Show

Sleep 1s
Type `npx delimit-cli remember "Auth uses session cookies, not JWT. Decided after the token-leak review."`
Sleep 500ms
Enter
Sleep 3s

Type "npx delimit-cli recall auth"
Sleep 500ms
Enter
Sleep 7s
