# Copyright 2026 Eudaemonic Enterprises Inc.
"""What a shell needs to work with ShelfFind: a key that opens the panel, and completion."""

KEYS = {"ctrl-t": r"\C-t", "ctrl-f": r"\C-f", "ctrl-g": r"\C-g", "alt-f": r"\ef", "alt-s": r"\es"}

BASH_INIT = r"""# ShelfFind: @KEY@ opens the search panel under the prompt. Enter opens the chosen
# document; Tab puts its address on the command line. Add to ~/.bashrc:
#   eval "$(shelffind init bash)"
__shelffind_panel() {
  local chosen
  chosen="$(command shelffind pick --widget --enter open --quote)" || return 0
  [ -n "$chosen" ] || return 0
  READLINE_LINE="${READLINE_LINE:0:READLINE_POINT}${chosen} ${READLINE_LINE:READLINE_POINT}"
  READLINE_POINT=$(( READLINE_POINT + ${#chosen} + 1 ))
}
bind -x '"@SEQUENCE@": __shelffind_panel'
"""

BASH_COMPLETION = r"""# Bash completion for shelffind, made by 'shelffind completion bash'.
_shelffind() {
  local current="${COMP_WORDS[COMP_CWORD]}" previous="${COMP_WORDS[COMP_CWORD-1]}"
  local command="" word
  for word in "${COMP_WORDS[@]:1:COMP_CWORD-1}"; do
    case "$word" in -*) ;; *) command="$word"; break ;; esac
  done
  case "$previous" in
    --where) COMPREPLY=( $(compgen -W "all name text" -- "$current") ); return ;;
    --enter) COMPREPLY=( $(compgen -W "open print" -- "$current") ); return ;;
    --data-dir) COMPREPLY=( $(compgen -d -- "$current") ); return ;;
  esac
  case "$command" in
    "") COMPREPLY=( $(compgen -W "@COMMANDS@ --version --data-dir --help" -- "$current") ) ;;
    roots) COMPREPLY=( $(compgen -W "add remove list" -- "$current") $(compgen -d -- "$current") ) ;;
    sources) COMPREPLY=( $(compgen -W "add remove list" -- "$current") ) ;;
    config) COMPREPLY=( $(compgen -W "show path check" -- "$current") ) ;;
    init|completion) COMPREPLY=( $(compgen -W "bash" -- "$current") ) ;;
    search|pick) COMPREPLY=( $(compgen -W "--json --list --exact --where --source --root --format --limit" -- "$current") ) ;;
    index) COMPREPLY=( $(compgen -W "--names-only --ocr --lang --retry --limit --root --file" -- "$current") ) ;;
    status) COMPREPLY=( $(compgen -W "--json --errors" -- "$current") ) ;;
    open|apps|reveal) COMPREPLY=( $(compgen -f -- "$current") ) ;;
  esac
}
complete -o filenames -F _shelffind shelffind
"""


def init(shell, key="ctrl-t"):
    if shell != "bash":
        raise ValueError(f"No key binding is written for {shell} yet; bash is supported")
    if key not in KEYS:
        raise ValueError(f"Choose the key among {', '.join(KEYS)}")
    return BASH_INIT.replace("@KEY@", key.title()).replace("@SEQUENCE@", KEYS[key])


def completion(shell, commands):
    if shell != "bash":
        raise ValueError(f"No completion is written for {shell} yet; bash is supported")
    return BASH_COMPLETION.replace("@COMMANDS@", " ".join(commands))
