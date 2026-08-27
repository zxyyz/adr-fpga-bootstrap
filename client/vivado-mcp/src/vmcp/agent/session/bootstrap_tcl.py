r"""The Tcl side of the session protocol.

Kept as a Python string rather than a data file so the zipapp needs no resource
loading.  Sourced by the tool at startup; from then on the daemon drives the
interpreter by writing one line per command on stdin::

    vmcp_eval <uuid> <base64 of the utf-8 script>\n

and reads back::

    ...arbitrary tool log output...
    <<<VMCP:BEGIN:uuid>>>
    {"rc":0,"result":"...","errorinfo":"..."}
    <<<VMCP:END:uuid>>>

base64 on the way in keeps us out of Tcl quoting entirely: the script may
contain unbalanced braces, quotes, ``$``, newlines or non-ASCII with no
escaping.  A single line of JSON on the way out means the daemon needs no Tcl
list parser.  Anything printed outside a BEGIN/END pair is log output.

Vivado 2025.2 ships Tcl 8.6.13, so ``binary decode base64`` is available.
"""

BEGIN = "<<<VMCP:BEGIN:"
END = "<<<VMCP:END:"
READY = "<<<VMCP:READY>>>"
SENTINEL_SUFFIX = ">>>"

BOOTSTRAP_TCL = r"""# vmcp session bootstrap -- generated, do not edit by hand.

fconfigure stdout -encoding utf-8 -translation lf
fconfigure stdin  -encoding utf-8 -translation lf

namespace eval ::vmcp {
    # JSON escape table. Built at boot so *every* control character is covered;
    # an unescaped 0x00-0x1f would make the payload line invalid JSON.
    variable jmap [list \\ {\\} \" {\"} \n {\n} \r {\r} \t {\t} \b {\b} \f {\f}]
    for {set i 0} {$i < 32} {incr i} {
        set ch [format %c $i]
        if {[lsearch -exact $jmap $ch] >= 0} continue
        lappend jmap $ch [format {\u%04x} $i]
    }
    unset i ch
}

proc ::vmcp::jesc {s} {
    variable jmap
    return [string map $jmap $s]
}

proc ::vmcp::emit {uuid rc result errorinfo} {
    puts stdout "<<<VMCP:BEGIN:$uuid>>>"
    puts stdout "{\"rc\":$rc,\"result\":\"[::vmcp::jesc $result]\",\"errorinfo\":\"[::vmcp::jesc $errorinfo]\"}"
    puts stdout "<<<VMCP:END:$uuid>>>"
    flush stdout
}

proc vmcp_eval {uuid b64} {
    set script [encoding convertfrom utf-8 [binary decode base64 $b64]]
    # uplevel #0 so the script sees global scope, matching what a human typing
    # at the Vivado prompt would get.
    set rc [catch {uplevel #0 $script} res opt]
    # A top-level `return` completes with TCL_RETURN (2), which is success as far
    # as the caller is concerned. Only TCL_ERROR (1) is a failure.
    if {$rc == 2} {
        set rc 0
    }
    set ei ""
    if {$rc} {
        catch {set ei [dict get $opt -errorinfo]}
    }
    ::vmcp::emit $uuid $rc $res $ei
    return
}

puts stdout "<<<VMCP:READY>>>"
flush stdout
"""
