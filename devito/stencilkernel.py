from __future__ import absolute_import

import operator
from collections import OrderedDict, namedtuple
from ctypes import c_double, c_int
from functools import reduce
from itertools import combinations

import cgen as c
import numpy as np

from devito.cgen_utils import Allocator, blankline
from devito.compiler import (get_compiler_from_env, jit_compile, load)
from devito.dimension import BufferedDimension, Dimension, time
from devito.dle import filter_iterations, transform
from devito.dse import (estimate_cost, estimate_memory, indexify, rewrite)
from devito.interfaces import SymbolicData, Forward, Backward
from devito.logger import bar, error, info, info_at
from devito.nodes import (Element, Expression, Function, Iteration, List,
                          LocalExpression, TimedList)
from devito.profiler import Profiler
from devito.tools import (SetOrderedDict, as_tuple, filter_ordered, filter_sorted,
                          flatten, partial_order)
from devito.visitors import (FindNodes, FindSections, FindSymbols, FindScopes,
                             IsPerfectIteration, MergeOuterIterations,
                             ResolveIterationVariable, SubstituteExpression, Transformer)
from devito.exceptions import InvalidArgument

__all__ = ['StencilKernel']


class OperatorBasic(Function):

    _default_headers = ['#define _POSIX_C_SOURCE 200809L']
    _default_includes = ['stdlib.h', 'math.h', 'sys/time.h']

    """A special :class:`Function` to generate and compile C code evaluating
    an ordered sequence of stencil expressions.

    :param stencils: SymPy equation or list of equations that define the
                     stencil used to create the kernel of this Operator.
    :param kwargs: Accept the following entries: ::

        * name : Name of the kernel function - defaults to "Kernel".
        * subs : Dict or list of dicts containing the SymPy symbol
                 substitutions for each stencil respectively.
        * time_axis : :class:`TimeAxis` object to indicate direction in which
                      to advance time during computation.
        * dse : Use the Devito Symbolic Engine to optimize the expressions -
                defaults to "advanced".
        * dle : Use the Devito Loop Engine to optimize the loops -
                defaults to "advanced".
        * compiler: Compiler class used to perform JIT compilation.
                    If not provided, the compiler will be inferred from the
                    environment variable DEVITO_ARCH, or default to GNUCompiler.
    """
    def __init__(self, stencils, **kwargs):
        self.name = kwargs.get("name", "Kernel")
        subs = kwargs.get("subs", {})
        time_axis = kwargs.get("time_axis", Forward)
        dse = kwargs.get("dse", "advanced")
        dle = kwargs.get("dle", "advanced")

        # Default attributes required for compilation
        self._headers = list(self._default_headers)
        self._includes = list(self._default_includes)
        self._lib = None
        self._cfunction = None

        # Set the direction of time acoording to the given TimeAxis
        time.reverse = time_axis == Backward

        # Normalize the collection of stencils
        stencils = [indexify(s) for s in as_tuple(stencils)]
        stencils = [s.xreplace(subs) for s in stencils]

        # Retrieve the data type of the Operator
        self.dtype = self._retrieve_dtype(stencils)

        # Apply the Devito Symbolic Engine for symbolic optimization
        dse_state = rewrite(stencils, mode=dse)

        # Wrap expressions with Iterations according to dimensions
        nodes = self._schedule_expressions(dse_state)

        # Introduce C-level profiling infrastructure
        self.sections = OrderedDict()
        nodes = self._profile_sections(nodes)

        # Parameters of the Operator (Dimensions necessary for data casts)
        parameters = FindSymbols('kernel-data').visit(nodes)
        dimensions = FindSymbols('dimensions').visit(nodes)
        dimensions += [d.parent for d in dimensions if d.is_Buffered]
        parameters += filter_ordered([d for d in dimensions if d.size is None],
                                     key=operator.attrgetter('name'))

        # Resolve and substitute dimensions for loop index variables
        subs = {}
        nodes = ResolveIterationVariable().visit(nodes, subs=subs)
        nodes = SubstituteExpression(subs=subs).visit(nodes)

        # Apply the Devito Loop Engine for loop optimization
        dle_state = transform(nodes, set_dle_mode(dle, self.compiler), self.compiler)
        parameters += [i.argument for i in dle_state.arguments]
        self._includes.extend(list(dle_state.includes))

        # Introduce all required C declarations
        nodes, elemental_functions = self._insert_declarations(dle_state, parameters)
        self.elemental_functions = elemental_functions

        # Track the DSE and DLE output, as they may be useful later
        self._dse_state = dse_state
        self._dle_state = dle_state

        # Finish instantiation
        super(OperatorBasic, self).__init__(self.name, nodes, 'int', parameters, ())

    def arguments(self, *args, **kwargs):
        """
        Return the arguments necessary to apply the Operator.
        """
        if len(args) == 0:
            args = self.parameters

        # Will perform auto-tuning if the user requested it and loop blocking was used
        maybe_autotune = kwargs.get('autotune', False)

        arguments = OrderedDict([(arg.name, arg) for arg in self.parameters])
        dim_sizes = {}

        # Have we been provided substitutes for symbol data?
        # Only SymbolicData can be overridden with this route
        r_args = [f_n for f_n, f in arguments.items() if isinstance(f, SymbolicData)]
        o_vals = OrderedDict([arg for arg in kwargs.items() if arg[0] in r_args])

        # Replace the overridden values with the provided ones
        for argname in o_vals.keys():
            if not arguments[argname].shape == o_vals[argname].shape:
                raise InvalidArgument("Shapes must match")

            arguments[argname] = o_vals[argname]

        # Traverse positional args and infer loop sizes for open dimensions
        f_args = [f for f in arguments.values() if isinstance(f, SymbolicData)]
        for f, arg in zip(f_args, args):
            arguments[arg.name] = self._arg_data(f)
            shape = self._arg_shape(f)

            # Ensure data dimensions match symbol dimensions
            for i, dim in enumerate(f.indices):
                # Infer open loop limits
                if dim.size is None:
                    # First, try to find dim size in kwargs
                    if dim.name in kwargs:
                        dim_sizes[dim] = kwargs[dim.name]

                    if dim in dim_sizes:
                        # Ensure size matches previously defined size
                        if not dim.is_Buffered:
                            assert dim_sizes[dim] <= shape[i]
                    else:
                        # Derive size from grid data shape and store
                        dim_sizes[dim] = shape[i]
                else:
                    if not isinstance(dim, BufferedDimension):
                        assert dim.size == shape[i]

        # Ensure parent for buffered dims is defined
        buf_dims = [d for d in dim_sizes if d.is_Buffered]
        for dim in buf_dims:
            if dim.parent not in dim_sizes:
                dim_sizes[dim.parent] = dim_sizes[dim]

        # Add user-provided block sizes, if any
        dle_arguments = OrderedDict()
        for i in self._dle_state.arguments:
            dim_size = dim_sizes.get(i.original_dim, i.original_dim.size)
            assert dim_size is not None, "Unable to match arguments and values"
            if i.value:
                try:
                    dle_arguments[i.argument] = i.value(dim_size)
                except TypeError:
                    dle_arguments[i.argument] = i.value
                    # User-provided block size available, do not autotune
                    maybe_autotune = False
            else:
                dle_arguments[i.argument] = dim_size
        dim_sizes.update(dle_arguments)

        # Insert loop size arguments from dimension values
        d_args = [d for d in arguments.values() if isinstance(d, Dimension)]
        for d in d_args:
            arguments[d.name] = dim_sizes[d]

        # Might have been asked to auto-tune the block size
        if maybe_autotune:
            self._autotune(arguments)

        # Add profiler structs
        arguments.update(self._extra_arguments())
        return arguments, dim_sizes

    @property
    def ccode(self):
        """Returns the C code generated by this kernel.

        This function generates the internal code block from Iteration
        and Expression objects, and adds the necessary template code
        around it.
        """
        # Generate function body with all the trimmings
        body = [e.ccode for e in self.body]
        ret = [c.Statement("return 0")]
        kernel = c.FunctionBody(self._ctop, c.Block(self._ccasts + body + ret))

        # Generate elemental functions produced by the DLE
        elemental_functions = [e.ccode for e in self.elemental_functions]
        elemental_functions += [blankline]

        # Generate file header with includes and definitions
        header = [c.Line(i) for i in self._headers]
        includes = [c.Include(i, system=False) for i in self._includes]
        includes += [blankline]

        return c.Module(header + includes + self._cglobals +
                        elemental_functions + [kernel])

    @property
    def compile(self):
        """
        JIT-compile the Operator.

        Note that this invokes the JIT compilation toolchain with the compiler
        class derived in the constructor. Also, JIT compilation it is ensured that
        JIT compilation will only be performed once per Operator, reagardless of
        how many times this method is invoked.

        :returns: The file name of the JIT-compiled function.
        """
        if self._lib is None:
            # No need to recompile if a shared object has already been loaded.
            return jit_compile(self.ccode, self.compiler)
        else:
            return self._lib.name

    @property
    def cfunction(self):
        """Returns the JIT-compiled C function as a ctypes.FuncPtr object."""
        if self._lib is None:
            basename = self.compile
            self._lib = load(basename, self.compiler)
            self._lib.name = basename

        if self._cfunction is None:
            self._cfunction = getattr(self._lib, self.name)
            argtypes = [c_int if isinstance(v, Dimension) else
                        np.ctypeslib.ndpointer(dtype=v.dtype, flags='C')
                        for v in self.parameters]
            self._cfunction.argtypes = argtypes

        return self._cfunction

    def _arg_data(self, argument):
        return None

    def _arg_shape(self, argument):
        return argument.shape

    def _extra_arguments(self):
        return {}

    def _profile_sections(self, nodes):
        """Introduce C-level profiling nodes within the Iteration/Expression tree."""
        return nodes

    def _profile_summary(self, dim_sizes):
        """
        Produce a summary of the performance achieved
        """
        return PerformanceSummary()

    def _autotune(self, arguments):
        """Use auto-tuning on this Operator to determine empirically the
        best block sizes (when loop blocking is in use). The block sizes tested
        are those listed in ``options['at_blocksizes']``, plus the case that is
        as if blocking were not applied (ie, unitary block size)."""
        pass

    def _schedule_expressions(self, dse_state):
        """Wrap :class:`Expression` objects within suitable hierarchies of
        :class:`Iteration` according to dimensions.
        """
        functions = flatten(Expression(i).functions for i in dse_state.input)
        ordering = partial_order([i.indices for i in functions])

        processed = []
        for cluster in dse_state.clusters:
            # Build declarations or assignments
            body = [Expression(v, np.int32 if cluster.is_index(k) else self.dtype)
                    for k, v in cluster.items()]
            offsets = SetOrderedDict.union(*[i.index_offsets for i in body])

            # Filter out aliasing due to buffered dimensions
            key = lambda d: d.parent if d.is_Buffered else d
            dimensions = filter_ordered(list(offsets.keys()), key=key)

            # Determine a total ordering for the dimensions
            dimensions = filter_sorted(dimensions, key=lambda d: ordering.index(d))
            for d in reversed(dimensions):
                body = Iteration(body, dimension=d, limits=d.size, offsets=offsets[d])
            processed.append(body)

        # Merge Iterations iff outermost iterations agree
        processed = MergeOuterIterations().visit(processed)

        # Remove temporaries became redundat after squashing Iterations
        mapper = {}
        for k, v in FindSections().visit(processed).items():
            candidate = k[-1]
            if not IsPerfectIteration().visit(candidate):
                continue
            found = set()
            trimmed = []
            for n in v:
                if n.is_Expression:
                    if n.stencil not in found:
                        trimmed.append(n)
                        found.add(n.stencil)
                else:
                    trimmed.append(n)
            mapper[candidate] = Iteration(trimmed, **candidate.args_frozen)
        processed = Transformer(mapper).visit(processed)

        return processed

    def _insert_declarations(self, dle_state, parameters):
        """Populate the Operator's body with the required array and
        variable declarations, to generate a legal C file."""

        nodes = dle_state.nodes

        # Resolve function calls first
        scopes = []
        for k, v in FindScopes().visit(nodes).items():
            if k.is_FunCall:
                function = dle_state.func_table[k.name]
                scopes.extend(FindScopes().visit(function, queue=list(v)).items())
            else:
                scopes.append((k, v))

        # Determine all required declarations
        allocator = Allocator()
        mapper = OrderedDict()
        for k, v in scopes:
            if k.is_scalar:
                # Inline declaration
                mapper[k] = LocalExpression(**k.args)
            elif k.output_function._mem_external:
                # Nothing to do, variable passed as kernel argument
                continue
            elif k.output_function._mem_stack:
                # On the stack, as established by the DLE
                key = lambda i: i.dim not in k.output_function.indices
                site = filter_iterations(v, key=key, stop='consecutive')
                allocator.push_stack(site[-1], k.output_function)
            else:
                # On the heap, as a tensor that must be globally accessible
                allocator.push_heap(k.output_function)

        # Introduce declarations on the stack
        for k, v in allocator.onstack:
            allocs = as_tuple([Element(i) for i in v])
            mapper[k] = Iteration(allocs + k.nodes, **k.args_frozen)
        nodes = Transformer(mapper).visit(nodes)
        elemental_functions = Transformer(mapper).visit(dle_state.elemental_functions)

        # Introduce declarations on the heap (if any)
        if allocator.onheap:
            decls, allocs, frees = zip(*allocator.onheap)
            nodes = List(header=decls + allocs, body=nodes, footer=frees)

        return nodes, elemental_functions

    def _retrieve_dtype(self, stencils):
        """
        Retrieve the data type of a set of stencils. Raise an error if there
        is no common data type (ie, if at least one stencil differs in the
        data type).
        """
        lhss = set([s.lhs.base.function.dtype for s in stencils])
        if len(lhss) != 1:
            raise RuntimeError("Stencil types mismatch.")
        return lhss.pop()

    @property
    def _cparameters(self):
        return super(OperatorBasic, self)._cparameters

    @property
    def _cglobals(self):
        return []


class OperatorForeign(OperatorBasic):
    """
    A special :class:`OperatorBasic` for use outside of Python.
    """

    def arguments(self, *args, **kwargs):
        arguments, _ = super(OperatorForeign, self).arguments(*args, **kwargs)
        return arguments.items()


class OperatorCore(OperatorBasic):
    """
    A special :class:`OperatorBasic` that, besides generation and compilation of
    C code evaluating stencil expressions, can also execute the computation.
    """

    def __init__(self, stencils, **kwargs):
        self.profiler = Profiler(self.compiler.openmp)
        super(OperatorCore, self).__init__(stencils, **kwargs)

    def __call__(self, *args, **kwargs):
        self.apply(*args, **kwargs)

    def apply(self, *args, **kwargs):
        """Apply the stencil kernel to a set of data objects"""
        # Build the arguments list to invoke the kernel function
        arguments, dim_sizes = self.arguments(*args, **kwargs)

        # Invoke kernel function with args
        self.cfunction(*list(arguments.values()))

        # Output summary of performance achieved
        summary = self._profile_summary(dim_sizes)
        with bar():
            for k, v in summary.items():
                name = '%s<%s>' % (k, ','.join('%d' % i for i in v.itershape))
                info("Section %s with OI=%.2f computed in %.3f s [Perf: %.2f GFlops/s]" %
                     (name, v.oi, v.time, v.gflopss))

        return summary

    def _arg_data(self, argument):
        # Ensure we're dealing or deriving numpy arrays
        data = argument.data
        if not isinstance(data, np.ndarray):
            error('No array data found for argument %s' % argument.name)
        return data

    def _arg_shape(self, argument):
        return argument.data.shape

    def _profile_sections(self, nodes):
        """Introduce C-level profiling nodes within the Iteration/Expression tree."""
        mapper = {}
        for i, expr in enumerate(nodes):
            for itspace in FindSections().visit(expr).keys():
                for j in itspace:
                    if IsPerfectIteration().visit(j) and j not in mapper:
                        # Insert `TimedList` block. This should come from
                        # the profiler, but we do this manually for now.
                        lname = 'loop_%s_%d' % (j.index, len(mapper))
                        mapper[j] = TimedList(gname=self.profiler.t_name,
                                              lname=lname, body=j)
                        self.profiler.t_fields += [(lname, c_double)]

                        # Estimate computational properties of the timed section
                        # (operational intensity, memory accesses)
                        expressions = FindNodes(Expression).visit(j)
                        ops = estimate_cost([e.stencil for e in expressions])
                        memory = estimate_memory([e.stencil for e in expressions])
                        self.sections[itspace] = Profile(lname, ops, memory)
                        break
        processed = Transformer(mapper).visit(List(body=nodes))
        return processed

    def _profile_summary(self, dim_sizes):
        """
        Produce a summary of the performance achieved
        """
        summary = PerformanceSummary()
        for itspace, profile in self.sections.items():
            dims = {i: i.dim.parent if i.dim.is_Buffered else i.dim for i in itspace}

            # Time
            time = self.profiler.timings[profile.timer]

            # Flops
            itershape = [i.extent(finish=dim_sizes.get(dims[i])) for i in itspace]
            iterspace = reduce(operator.mul, itershape)
            flops = float(profile.ops*iterspace)
            gflops = flops/10**9

            # Compulsory traffic
            datashape = [i.dim.size or dim_sizes[dims[i]] for i in itspace]
            dataspace = reduce(operator.mul, datashape)
            traffic = profile.memory*dataspace*self.dtype().itemsize

            # Derived metrics
            oi = flops/traffic
            gflopss = gflops/time

            # Keep track of performance achieved
            summary.setsection(profile.timer, time, gflopss, oi, itershape, datashape)

        # Rename the most time consuming section as 'main'
        summary['main'] = summary.pop(max(summary, key=summary.get))

        return summary

    def _extra_arguments(self):
        return OrderedDict([(self.profiler.s_name,
                             self.profiler.as_ctypes_pointer(Profiler.TIME))])

    def _autotune(self, arguments):
        """Use auto-tuning on this Operator to determine empirically the
        best block sizes (when loop blocking is in use). The block sizes tested
        are those listed in ``options['at_blocksizes']``, plus the case that is
        as if blocking were not applied (ie, unitary block size)."""
        if not self._dle_state.has_applied_blocking:
            return

        at_arguments = arguments.copy()

        # Output data must not be changed
        output = [i.base.label.name for i in self._dse_state.output_fields]
        for k, v in arguments.items():
            if k in output:
                at_arguments[k] = v.copy()

        # Squeeze dimensions to minimize auto-tuning time
        iterations = FindNodes(Iteration).visit(self.body)
        squeezable = [i.dim.parent.name for i in iterations
                      if i.is_Sequential and i.dim.is_Buffered]

        # Attempted block sizes
        mapper = OrderedDict([(i.argument.name, i) for i in self._dle_state.arguments])
        blocksizes = [OrderedDict([(i, v) for i in mapper])
                      for v in options['at_blocksize']]
        if self._dle_state.needs_aggressive_autotuning:
            elaborated = []
            for blocksize in list(blocksizes)[:3]:
                for i in list(blocksizes):
                    handle = i.items()[-1]
                    elaborated.append(OrderedDict(blocksize.items()[:-1] + [handle]))
            for blocksize in list(blocksizes):
                ncombs = len(blocksize)
                for i in range(ncombs):
                    for j in combinations(blocksize, i+1):
                        handle = [(k, blocksize[k]*2 if k in j else v)
                                  for k, v in blocksize.items()]
                        elaborated.append(OrderedDict(handle))
            blocksizes.extend(elaborated)

        # Note: there is only a single loop over 'blocksize' because only
        # square blocks are tested
        timings = OrderedDict()
        for blocksize in blocksizes:
            illegal = False
            for k, v in at_arguments.items():
                if k in blocksize:
                    val = blocksize[k]
                    handle = at_arguments.get(mapper[k].original_dim.name)
                    if val <= mapper[k].iteration.end(handle):
                        at_arguments[k] = val
                    else:
                        # Block size cannot be larger than actual dimension
                        illegal = True
                        break
                elif k in squeezable:
                    at_arguments[k] = options['at_squeezer']
            if illegal:
                continue

            # Add profiler structs
            at_arguments.update(self._extra_arguments())

            self.cfunction(*list(at_arguments.values()))
            elapsed = sum(self.profiler.timings.values())
            timings[tuple(blocksize.items())] = elapsed
            info_at("<%s>: %f" %
                    (','.join('%d' % i for i in blocksize.values()), elapsed))

        best = dict(min(timings, key=timings.get))
        for k, v in arguments.items():
            if k in mapper:
                arguments[k] = best[k]

        info('Auto-tuned block shape: %s' % best)

    @property
    def _cparameters(self):
        cparameters = super(OperatorCore, self)._cparameters
        cparameters += [c.Pointer(c.Value('struct %s' % self.profiler.s_name,
                                          self.profiler.t_name))]
        return cparameters

    @property
    def _cglobals(self):
        return [self.profiler.as_cgen_struct(Profiler.TIME), blankline]


class StencilKernel(object):

    def __new__(cls, *args, **kwargs):
        # What type of Operator should I return ?
        cls = OperatorForeign if kwargs.pop('external', False) else OperatorCore

        # Trigger instantiation
        obj = cls.__new__(cls, *args, **kwargs)
        obj.compiler = kwargs.pop("compiler", get_compiler_from_env())
        obj.__init__(*args, **kwargs)
        return obj


# Helpers for performance tracking

"""
A helper to return structured performance data.
"""
PerfEntry = namedtuple('PerfEntry', 'time gflopss oi itershape datashape')


class PerformanceSummary(OrderedDict):

    """
    A special dictionary to track and view performance data.
    """

    def setsection(self, key, time, gflopss, oi, itershape, datashape):
        self[key] = PerfEntry(time, gflopss, oi, itershape, datashape)

    @property
    def gflopss(self):
        return OrderedDict([(k, v.gflopss) for k, v in self.items()])

    @property
    def oi(self):
        return OrderedDict([(k, v.oi) for k, v in self.items()])

    @property
    def timings(self):
        return OrderedDict([(k, v.time) for k, v in self.items()])


# StencilKernel options and name conventions

"""
A dict of standard names to be used for code generation
"""
cnames = {
    'loc_timer': 'loc_timer',
    'glb_timer': 'glb_timer'
}

"""
StencilKernel options
"""
options = {
    'at_squeezer': 3,
    'at_blocksize': [8, 16, 24, 32, 40, 64, 128]
}

"""
A helper to track profiled sections of code.
"""
Profile = namedtuple('Profile', 'timer ops memory')


# Helpers to use a StencilKernel

def set_dle_mode(mode, compiler):
    """
    Transform :class:`StencilKernel` input in a format understandable by the DLE.
    """
    if not mode:
        return 'noop'
    mode = as_tuple(mode)
    params = mode[-1]
    if isinstance(params, dict):
        params['openmp'] = compiler.openmp
    else:
        params = {'openmp': compiler.openmp}
        mode += (params,)
    return mode