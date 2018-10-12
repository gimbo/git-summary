# git-summary

This tool quickly and prettily summarises the state of all the git repos in some folder (non-recursively).  The summary
tells you which branch is checked out, and how dirty the repo is.

The motivation for writing it was that I was working on a project involving a number of microservices, so I had a folder
with about 20 repos in it and I wanted a good way to summarise their respective states.

It has some nice features:

  - For speed, queries each repo concurrently (may be disabled).
  - For prettiness, updates each repo's output line concurrently using fancy ANSI shiz (may be disabled).
  - For speed, doesn't fetch from remotes by default (but can).
  - For prettiness, coloured output using fancy ANSI shiz (may be disabled, for monochrome output).
  - For convenience, disables all the fancy ANSI shiz automatically if it detects that output is being piped out.
  - For convenience, a default target path can specified in an env var (was handy in my microservices case).

The combination of the first two features (concurrent queries and concurrent output updates) means that by default the
tool is rather fast and feels pleasantly responsive.  Either of these features may be disabled if they're causing
problems; if they're both disabled it simply trudges through the repos one at a time, writing output as it goes, like a
peasant.

The built-in help is extensive, so either run the tool with the `--help` flag or examine the source for more details.

## Example output

[![asciicast](https://asciinema.org/a/206565.png)](https://asciinema.org/a/206565)

## Requirements

It's python 3; it might work under 2 but I haven't tested it.  (Only got python2 on your system?  Let
[pyenv](https://github.com/pyenv/pyenv) into your life and make things better.)  Not sure what the minimum version is.

## Installation

You could just download this repo and do `setup.py install`.  I recommend using
[pipsi](https://github.com/mitsuhiko/pipsi), however.

## Caveats

  - I use this in iTerm2 under MacOS and it works well. In other terminals and other operating systems, the "fancy
    ANSI shiz" may be less good, in particular the thing that tries to work out the cursor's position when the
    program starts (which is needed to get the fancy updates working) might fail.  But that's OK, you can always
    just disable it or fix it.  :-)
    
  - [git-extras](https://github.com/tj/git-extras) contains a "git-summary" tool which does something entirely
    different.  Oops, my bad.  Ah well, never mind...

--

Andy Gimblett
October 2018 (though the codebase is a couple of years old now)
