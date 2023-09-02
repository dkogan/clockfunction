#!/usr/bin/python3

r"""This is a simple tool to run an application under perf to measure the runtime
of given functions.

This program runs an arbitrary application with arbitrary args (cmd arg0 arg1
arg2 ...) with some (very light) instrumentation. This instrumentation traces
entries and exits to a given set of functions, and measures the elapsed time
spent. When the application exits, basic statistics are printed to let the user
know how fast or slow the queried functions are.

Each function is specified as a 'func@lib' string (ltrace-style). 'func' is the
name of the function we care about. This could be a shell pattern to pick out
multiple functions. 'lib' is the ELF library or executable that contains this
function; must be an absolute path.

Example:

  $ ./clockfunction.py '*rand*'@/usr/bin/perl perl_run@/usr/bin/perl perl -e 'for $i (0..100000) { $s = rand(); }'

  ## All timings in seconds
  # function mean min max stdev Ncalls
  Perl_drand48_init_r 7.55896326154e-06 7.55896326154e-06 7.55896326154e-06 0.0               1
  Perl_drand48_r      1.95271501819e-06 1.76404137164e-06 3.67719912902e-05 4.0105865074e-07  100001
  Perl_pp_rand        5.23026800056e-06 4.78199217469e-06 0.000326015986502 1.71576428687e-06 100001
  perl_run            0.662568764063    0.662568764063    0.662568764063    0.0               1

The table was re-spaced for readability. So we see that the main perl
application took 0.66 seconds. And Perl_pp_rand was called 100001 times, taking
5.23us each time, on average, for a total of 0.523 seconds. A lower-level
Perl_drand48_r function took about 1/3 of the time of Perl_pp_rand. If one cared
about this detail of perl, this would be very interesting to know. And we found
it out without any compile-time instrumentation of our binary and without even
bothering to find out what the rand functions area called.

Recursive or parallel invocations are supported so far as the mean and Ncalls
will be reported correctly. The min, max and stdev of the timings will not be
available, however.

CAVEATS

This tool is a quick hack, and all the actual work is done by 'perf'. This tool
calls 'sudo' all over the place, which is ugly.

A relatively recent 'perf' is required. The devs have been tinkering with the
semantics of 'perf probe -F'. The following should produce reasonable output:

  perf probe -x `which python` -F 'Py*'

(I.e. it should print out a long list of instrumentable functions in the python
executable that start with 'Py'). Older versions of the 'perf' tool will barf
instead. Note that 'perf' is a userspace tool that lives in the linux kernel
source tree. And it doesn't directly depend on specific kernel versions.
Grabbing a very recent kernel tree and rebuilding JUST 'perf' usually works. And
you don't need to rebuild the kernel and reboot. Usually.

When instrumenting C++ functions you generally need to use the mangled symbol
names. At this time 'perf' has partial support for demangled names, but it's not
complete enough to work fully ('perf probe -F' can report demangled names, but
you can't insert probes with ':' in their names since ':' is already taken in
'perf probe' syntax). So I use 'perf probe --no-demangle', which again requires
a relatively recent 'perf'. If you aren't looking at C++, but your perf is too
old to have --no-demangle, you'll get needless barfing; take out the
'--no-demangle' in that case.

"""


import os
import os.path
import sys
import re
import fnmatch
import subprocess
import math

contexts = {}
perf     = "perf"


# These are called by "perf script" to process recorded events
def preamble(event_name, fields):
        global contexts

        t_now = float(fields['common_s']) + float(fields['common_ns']) / 1e9
        m     = re.match("probe_.*?__(.*?)(_ret)?(?:_[0-9]+|__return)?$", event_name)

        if m:
                func = m.group(1)
                if not func in contexts: contexts[func] = { 't_sum':        0,
                                                            'N_exits':      0,
                                                            'depth':        0,
                                                            'latencies':    [],
                                                            't_last_enter': None,
                                                            'uncertain_entry_exit': False}
                ctx = contexts[func]
                return ctx, t_now, func, m.group(2) is not None
        else:
                sys.stderr.write(f"Couldn't parse event probe name: '{event_name}'. Skipping event\n")
                return (None,None,None,None)

def trace_unhandled(event_name, perf_context, fields):
        # Each probe event is processed here. If recursion or parallelism
        # happens, I could see multiple function enter events before an exit. In
        # that case I can still compute the mean time correctly because
        #
        #   (t_exit0 - t_enter0) + (t_exit1 - t_enter1) =
        #   t_exit0 + t_exit1 - t_enter1 - t_enter0
        #
        # But the mean, min, max, stdev computations can't happen

        ctx, t_now, func, is_ret = preamble(event_name, fields)
        if ctx is None: return

        if is_ret:
                ctx['t_sum'  ] += t_now
                ctx['N_exits'] += 1
                ctx['depth'  ] -= 1

                if ctx['depth'] != 0:
                        if not ctx['uncertain_entry_exit']:
                                sys.stderr.write(f"Function {func} recursive or parallel. Cannot compute min, max, stdev\n")
                                ctx['uncertain_entry_exit'] = True
                else:
                        dt = t_now - ctx['t_last_enter']
                        ctx['t_last_enter'] = None
                        ctx['latencies']   += [dt]

        else:
                ctx['t_sum'  ]     -= t_now
                ctx['depth'  ]     += 1
                ctx['t_last_enter'] = t_now

                if ctx['depth'] != 1:
                        if not ctx['uncertain_entry_exit']:
                                sys.stderr.write(f"Function {func} recursive or parallel. Cannot compute min, max, stdev\n")
                                ctx['uncertain_entry_exit'] = True

def trace_end():

        print("## All timings in seconds")
        print("# function total mean min max stdev Ncalls")
        for func in sorted(contexts.keys()):

                ctx = contexts[func]

                # strip out the initial "func_"
                func = func[5:]

                if ctx['depth'] != 0:
                        if not ctx['uncertain_entry_exit']:
                                sys.stderr.write(f"Function {func} recursive or parallel: entry/exit counts don't balance. Cannot compute anything\n")
                        print(func,'- - - - - -')
                else:
                        if ctx['uncertain_entry_exit']:
                                print(func + ' ' + \
                                      ' '.join([f'{x:.2f}' \
                                                for x in (ctx['t_sum'],
                                                          ctx['t_sum']/ctx['N_exits'])]) +
                                      ' - - - ' + str(ctx['N_exits']))
                        else:
                                s = sum(ctx['latencies'])
                                N = len(ctx['latencies'])
                                m = s/N
                                var = sum( [(x-m)*(x-m) for x in ctx['latencies']]) / N
                                print(str(func) + ' ' + \
                                      ' '.join([f'{x:.2f}' \
                                                for x in (s,
                                                          m,
                                                          min(ctx['latencies']),
                                                          max(ctx['latencies']),
                                                          math.sqrt(var))]) +
                                               ' ' + str(N))




# These are called by the top-level script to add/remove probes and to collect
# and process data
def call( args, must_succeed=True, pass_output=False):
        if pass_output:
                proc = subprocess.Popen(args)
                proc.communicate()
                stdout,stderr = '',''
        else:
                proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='ascii')
                stdout,stderr = proc.communicate()
        if must_succeed and proc.returncode != 0:
                if not re.match('\w', stdout): stdout = ''
                if not re.match('\w', stderr): stderr = ''
                raise Exception(f"Failed when running {args}: {stdout + stderr}")
        return stdout,stderr

def get_functions_from_pattern(f_pattern, lib):
        try:
                out = subprocess.check_output( ('sudo', perf, 'probe', '-x', lib, '--no-demangle', '--filter', '*', '--funcs', f_pattern),
                                               encoding='ascii')
        except:
                raise Exception("Couldn't get the function list!")

        def accept(name):

                if not fnmatch.fnmatchcase(name, f_pattern):
                        return False
                if fnmatch.fnmatchcase(name, '*@plt'):
                        return False
                if fnmatch.fnmatchcase(name, '*_omp_fn*'):
                        return False
                return True
        def name_fname(name):
                # Apparently fname is limited to 64 characters. I cut down to 50
                # to leave room for the few more chars I will need
                #
                # need to prepend "func_" since some symbols start with
                # illegal characters; I THINK _ as the first character
                # doesn't work for perf probe names
                #
                # And I replace . with _, primarily for the .isra functions
                fname = "func_" + name.replace('.','_')
                if len(fname) > 50:
                        fname = fname[:50]
                return name,fname

        l = [name_fname(name) for name in out.splitlines() if accept(name) ]
        if len(l) == 0:
                raise Exception(f"Library {lib} found no functions matching pattern '{f_pattern}'")

        return l

def create_probes(funcslibs):

        # older perfs report failure if no probes exist and we try to delete
        # them all, so I allow this to fail
        call( ('sudo', perf, 'probe', '--del', '*'), must_succeed=False )

        # For C++ functions you must pass mangled function names, and I must
        # tell perf to not try to demangle anything. Currently (perf 4.9) perf
        # has a bug where demangling is half-done: 'perf probe --funcs' lists
        # demangled names, but you get an error if you try to use them. Upstream
        # knows about the bug, but hasn't yet fixed it
        for f_pattern,lib in funcslibs:
                lib = os.path.expanduser(lib)
                funcs = get_functions_from_pattern(f_pattern, lib)
                for f,fname in funcs:
                        print(f"## pattern: '{f_pattern}' in lib '{lib}' found funcs '{f}'")
                        try:
                                cmd1 = ('sudo', perf, 'probe', '-x', lib,
                                       '--no-demangle', '--add', f"{fname}={f}" )
                                cmd2 = ('sudo', perf, 'probe', '-x', lib,
                                       '--no-demangle', '--add', f"{fname}_ret={f}%return")
                                call(cmd1)
                                call(cmd2)
                        except:
                                print (f"## WARNING: Couldn't add probe for function '{f}' in library '{lib}'.\n" + \
                                       "## This possibly is OK. Continuing anyway")
                                print("## Command1: " + ' '.join(cmd1))
                                print("## Command2: " + ' '.join(cmd2))
                                print("##")

def get_all_probes():
        # older perfs report an error even when this succeeds, and older perfs
        # report on stderr. Thus I do this instead of calling check_output()
        stdout,stderr = call( ('sudo', perf, 'probe', '--list'), must_succeed=False, pass_output=False )
        out = stdout + stderr

        probes = []
        for l in out.splitlines():
                m = re.match('\s*(probe\S+)', l)
                if m is None:
                        # This can fail. Right now I see this
                        #
                        #   dima@fatty:~$ sudo perf_4.17 probe --list
                        #   Failed to find debug information for address 2290
                        #     probe_libmrcal:mrcal_distort (on mrcal_distort@dima/src_boats/mrcal/mrcal.c in /home/dima/src_boats/mrcal/libmrcal.so.0.0)
                        # The "Failed" line should be ignored
                        continue
                probes.append(m.group(1))
        return probes

def record_trace(fullcmd):
        # I trace all the probes I got. This is because I could have ended up
        # with more than one probe per function entry/exit, and I can't know
        # what I ended up with without asking perf
        probes = get_all_probes()
        probe_args = [f"-e{p}" for p in probes]

        probe_args = ('sudo', '-E', perf, 'record', '-o', 'perf.data', '-a') + tuple(probe_args) + tuple(fullcmd)
        call(probe_args, pass_output=True )
        call( ('sudo', 'chmod', 'a+r', "perf.data"))

def analyze_trace(this_script):
        call( (perf, 'script', '-s', this_script), pass_output=True )




if __name__ == '__main__':
        # When I run this via 'perf script' I STILL get here, even though I
        # don't want to run any of this in that case. So I check the executable
        # to see if 'perf' is running us
        pid      = os.getpid()
        exe_link = os.readlink(f"/proc/{pid}/exe" )
        if not re.match( "perf([-_].*)?$", os.path.basename( exe_link )):

                usage = f"Usage: {sys.argv[0]} func@lib [func@lib ...] cmd arg0 arg1 arg2 ...\n" + \
                        "\n" + __doc__
                if len(sys.argv) < 3:
                        print(usage)
                        sys.exit(1)

                # command is the first argument without a single '@'
                i_arg_cmd = next(i for i in range(1,len(sys.argv)) if not re.match('[^@]+@[^@]+$', sys.argv[i]))
                if i_arg_cmd < 2 or i_arg_cmd >= len(sys.argv):
                        print("No func@lib found in reasonable spot")
                        print("Usage: " + usage)
                        sys.exit(1)

                funcslibs = [arg.split('@') for arg in sys.argv[1:i_arg_cmd]]
                fullcmd   = sys.argv[i_arg_cmd:]

                create_probes(funcslibs)
                record_trace (fullcmd)
                sys.stdout.flush()
                analyze_trace(sys.argv[0])
