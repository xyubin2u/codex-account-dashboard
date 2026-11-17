"""Official CLI options used by the established local AGY launcher."""


def permission_defaults(args):
    values=list(args);safe='--safe' in values
    values=[value for value in values if value!='--safe']
    explicit=('--mode','--sandbox','--settings','--dangerously-skip-permissions')
    if safe or any(value==flag or value.startswith(flag+'=') for value in values for flag in explicit):return values
    if any(value in ('--help','-h','--version') for value in values):return values
    helpers={'acp','agent','agents','help','install','mcp','models','plugin','plugins',
             'remote-control','mic-serve','update','changelog'}
    value_flags={'--model','--effort','--agent','--add-dir','--conversation','--project',
                 '--log-file','--output-format','--input-format','--print','--prompt',
                 '--prompt-interactive','-p','-i','--json-schema','--print-timeout'}
    index=0
    while index<len(values):
        value=values[index]
        if value in value_flags:index+=2;continue
        if value.startswith('-'):index+=1;continue
        if value in helpers:return values
        break
    return ['--dangerously-skip-permissions',*values]
