from __future__ import print_function  # Python 2 and 3 print compatibility

from hail.expr.ast import *
from hail.expr.types import *
from hail.utils.java import *
from hail.utils.misc import plural
from hail.utils.linkedlist import LinkedList
from hail.genetics import Locus, Interval, Call
from hail.typecheck import *
from collections import Mapping


class Indices(object):
    @typecheck_method(source=anytype, axes=setof(strlike))
    def __init__(self, source=None, axes=set()):
        self.source = source
        self.axes = axes

    def __eq__(self, other):
        return isinstance(other, Indices) and self.source is other.source and self.axes == other.axes

    def __ne__(self, other):
        return not self.__eq__(other)

    @staticmethod
    def unify(*indices):
        axes = set()
        src = None
        for ind in indices:
            if src is None:
                src = ind.source
            else:
                if ind.source is not None and ind.source is not src:
                    raise ExpressionException()

            both = axes.intersection(ind.axes)

            axes = axes.union(ind.axes)

        return Indices(src, axes)

    def __str__(self):
        return 'Indices(axes={}, source={})'.format(self.axes, self.source)

    def __repr__(self):
        return 'Indices(axes={}, source={})'.format(repr(self.axes), repr(self.source))


class Aggregation(object):
    def __init__(self, indices, refs):
        self.indices = indices
        self.refs = refs


class Join(object):
    def __init__(self, join_function, temp_vars):
        self.join_function = join_function
        self.temp_vars = temp_vars


@typecheck(ast=AST, type=Type, indices=Indices, aggregations=LinkedList, joins=LinkedList, refs=LinkedList)
def construct_expr(ast, type, indices=Indices(), aggregations=LinkedList(Aggregation), joins=LinkedList(Join),
                   refs=LinkedList(tuple)):
    if isinstance(type, TArray) and type.element_type.__class__ in elt_typ_to_array_expr:
        return elt_typ_to_array_expr[type.element_type.__class__](ast, type, indices, aggregations, joins, refs)
    elif isinstance(type, TSet) and type.element_type.__class__ in elt_typ_to_set_expr:
        return elt_typ_to_set_expr[type.element_type.__class__](ast, type, indices, aggregations, joins, refs)
    elif type.__class__ in typ_to_expr:
        return typ_to_expr[type.__class__](ast, type, indices, aggregations, joins, refs)
    else:
        raise NotImplementedError(type)


@typecheck(name=strlike, type=Type, indices=Indices, prefix=nullable(strlike))
def construct_reference(name, type, indices, prefix=None):
    if prefix is not None:
        ast = Select(Reference(prefix, True), name)
    else:
        ast = Reference(name, True)
    return construct_expr(ast, type._deep_optional(), indices, refs=LinkedList(tuple).push((name, indices)))


def to_expr(e):
    if isinstance(e, Expression):
        return e
    elif isinstance(e, Aggregable):
        raise ExpressionException("Cannot use the result of 'agg.explode' or 'agg.filter' in expressions\n"
                                  "    These methods produce 'Aggregable' objects that can only be aggregated\n"
                                  "    with aggregator functions or used with further calls to 'agg.explode'\n"
                                  "    and 'agg.filter'. They support no other operations.")
    elif isinstance(e, str) or isinstance(e, unicode):
        return construct_expr(Literal('"{}"'.format(escape_str(e))), TString())
    elif isinstance(e, bool):
        return construct_expr(Literal("true" if e else "false"), TBoolean())
    elif isinstance(e, int):
        return construct_expr(Literal(str(e)), TInt32())
    elif isinstance(e, long):
        return construct_expr(ClassMethod('toInt64', Literal('"{}"'.format(e))), TInt64())
    elif isinstance(e, float):
        return construct_expr(Literal(str(e)), TFloat64())
    elif isinstance(e, Locus):
        return construct_expr(ApplyMethod('Locus', Literal('"{}"'.format(str(e)))), TLocus(e.reference_genome))
    elif isinstance(e, Interval):
        return construct_expr(ApplyMethod('LocusInterval', Literal('"{}"'.format(str(e)))),
                              TInterval(TLocus(e.reference_genome)))
    elif isinstance(e, Call):
        if e.ploidy == 0:
            return construct_expr(ApplyMethod('Call', to_expr(e.phased)._ast), TCall())
        elif e.ploidy == 1:
            return construct_expr(ApplyMethod('Call', to_expr(e.phased)._ast, to_expr(e[0])._ast), TCall())
        elif e.ploidy == 2:
            return construct_expr(ApplyMethod('Call', to_expr(e.phased)._ast, to_expr(e[0])._ast, to_expr(e[1])._ast), TCall())
        else:
            raise NotImplementedError("Do not support calls with ploidy == {}.".format(e.ploidy))
    elif isinstance(e, Struct):
        if len(e) == 0:
            return construct_expr(StructDeclaration([], []), TStruct([], []))
        attrs = e._fields.items()
        cols = [to_expr(x) for _, x in attrs]
        names = [k for k, _ in attrs]
        indices, aggregations, joins, refs = unify_all(*cols)
        t = TStruct(names, [col._type for col in cols])
        return construct_expr(StructDeclaration(names, [c._ast for c in cols]),
                              t, indices, aggregations, joins, refs)
    elif isinstance(e, list):
        if len(e) == 0:
            raise ExpressionException('Cannot convert empty list to expression.')
        cols = [to_expr(x) for x in e]
        types = list({col._type for col in cols})
        t = unify_types(*types)
        if not t:
            raise ExpressionException('Cannot convert list with heterogeneous key types to expression.'
                                      '\n    Found types: {}.'.format(types))
        indices, aggregations, joins, refs = unify_all(*cols)
        return construct_expr(ArrayDeclaration([col._ast for col in cols]),
                              TArray(t), indices, aggregations, joins, refs)
    elif isinstance(e, set):
        if len(e) == 0:
            raise ExpressionException('Cannot convert empty set to expression.')
        cols = [to_expr(x) for x in e]
        types = list({col._type for col in cols})
        t = unify_types(*types)
        if not t:
            raise ExpressionException('Cannot convert set with heterogeneous types to expression.'
                                      '\n    Found types: {}.'.format(types))
        indices, aggregations, joins, refs = unify_all(*cols)
        return construct_expr(ClassMethod('toSet', ArrayDeclaration([col._ast for col in cols])), TSet(t), indices,
                              aggregations, joins, refs)
    elif isinstance(e, dict):
        if len(e) == 0:
            raise ExpressionException('Cannot convert empty dictionary to expression.')
        key_cols = []
        value_cols = []
        keys = []
        values = []
        for k, v in e.items():
            key_cols.append(to_expr(k))
            keys.append(k)
            value_cols.append(to_expr(v))
            values.append(v)
        key_types = list({col._type for col in key_cols})
        value_types = list({col._type for col in value_cols})
        kt = unify_types(*key_types)
        if not kt:
            raise ExpressionException('Cannot convert dictionary with heterogeneous key types to expression.'
                                      '\n    Found types: {}.'.format(key_types))
        vt = unify_types(*value_types)
        if not vt:
            raise ExpressionException('Cannot convert dictionary with heterogeneous value types to expression.'
                                      '\n    Found types: {}.'.format(value_types))
        kc = to_expr(keys)
        vc = to_expr(values)

        indices, aggregations, joins, refs = unify_all(kc, vc)

        assert kt == kc._type.element_type
        assert vt == vc._type.element_type

        ast = ApplyMethod('Dict',
                          ArrayDeclaration([k._ast for k in key_cols]),
                          ArrayDeclaration([v._ast for v in value_cols]))
        return construct_expr(ast, TDict(kt, vt), indices, aggregations, joins, refs)
    else:
        raise ExpressionException("Cannot implicitly convert value '{}' of type '{}' to expression.".format(
            e, e.__class__))


_lazy_int32 = lazy()
_lazy_numeric = lazy()
_lazy_array = lazy()
_lazy_set = lazy()
_lazy_bool = lazy()
_lazy_struct = lazy()
_lazy_string = lazy()
_lazy_locus = lazy()
_lazy_interval = lazy()
_lazy_call = lazy()
_lazy_expr = lazy()

expr_int32 = transformed((_lazy_int32, identity),
                         (integral, to_expr))
expr_numeric = transformed((_lazy_numeric, identity),
                           (integral, to_expr),
                           (float, to_expr))
expr_list = transformed((list, to_expr),
                        (_lazy_array, identity))
expr_set = transformed((set, to_expr),
                       (_lazy_set, identity))
expr_bool = transformed((bool, to_expr),
                        (_lazy_bool, identity))
expr_struct = transformed((Struct, to_expr),
                          (_lazy_struct, identity))
expr_str = transformed((strlike, to_expr),
                       (_lazy_string, identity))
expr_locus = transformed((Locus, to_expr),
                         (_lazy_locus, identity))
expr_interval = transformed((Interval, to_expr),
                            (_lazy_interval, identity))
expr_call = transformed((Call, to_expr),
                        (_lazy_call, identity))
expr_any = transformed((_lazy_expr, identity),
                       (anytype, to_expr))


def unify_all(*exprs):
    assert len(exprs) > 0
    try:
        new_indices = Indices.unify(*[e._indices for e in exprs])
    except ExpressionException:
        # source mismatch
        from collections import defaultdict
        sources = defaultdict(lambda: [])
        for e in exprs:
            for name, inds in e._refs:
                sources[inds.source].append(str(name))
        raise ExpressionException("Cannot combine expressions from different source objects."
                                  "\n    Found fields from {n} objects:{fields}".format(
            n=len(sources),
            fields=''.join("\n        {}: {}".format(src, fds) for src, fds in sources.items())
        ))
    first, rest = exprs[0], exprs[1:]
    agg = first._aggregations
    joins = first._joins
    refs = first._refs
    for e in rest:
        agg = agg.push(*e._aggregations)
        joins = joins.push(*e._joins)
        refs = refs.push(*e._refs)
    return new_indices, agg, joins, refs


def unify_types_limited(*ts):
    classes = {t.__class__ for t in ts}
    if len(classes) == 1:
        # only one distinct class
        return ts[0]
    elif all(is_numeric(t) for t in ts):
        # assert there are at least 2 numeric types
        assert len(classes) > 1
        if TFloat64 in classes:
            return TFloat64()
        elif TFloat32 in classes:
            return TFloat32()
        else:
            assert classes == {TInt32, TInt64}
            return TInt64()
    else:
        return None


def unify_types(*ts):
    classes = {t.__class__ for t in ts}
    if len(classes) == 1:
        # only one distinct class
        return ts[0]
    elif all(is_numeric(t) for t in ts):
        # assert there are at least 2 numeric types
        assert len(classes) > 1
        if TFloat64 in classes:
            return TFloat64()
        elif TFloat32 in classes:
            return TFloat32()
        else:
            assert classes == {TInt32, TInt64}
            return TInt64()
    elif all(isinstance(t, TArray) for t in ts):
        et = unify_types(*(t.element_type for t in ts))
        if et:
            return TArray(et)
        else:
            return None
    else:
        return None


class Expression(object):
    """Base class for Hail expressions."""

    def _verify_sources(self):
        from collections import defaultdict
        sources = defaultdict(lambda: [])
        for name, inds in self._refs:
            sources[inds.source].append(str(name))

        if (len(sources.keys()) > 1):
            raise FatalError("verify: too many sources:\n  {}\n  {}\n  {}".format(sources, self._indices, self))

        if (len(sources.keys()) != 0 and sources.keys()[0] != self._indices.source):
            raise FatalError("verify: incompatible sources:\n  {}\n  {}\n  {}".format(sources, self._indices, self))

    @typecheck_method(ast=AST, type=Type, indices=Indices, aggregations=LinkedList, joins=LinkedList, refs=LinkedList)
    def __init__(self, ast, type, indices=Indices(), aggregations=LinkedList(Aggregation), joins=LinkedList(Join),
                 refs=LinkedList(tuple)):
        self._ast = ast
        self._type = type
        self._indices = indices
        self._aggregations = aggregations
        self._joins = joins
        self._refs = refs

        self._init()

        self._verify_sources()

    def __str__(self):
        return repr(self)

    def __repr__(self):
        s = "{super_repr}\n  Type: {type}".format(
            super_repr=super(Expression, self).__repr__(),
            type=str(self._type),
        )

        indices = self._indices
        if len(indices.axes) == 0:
            s += '\n  Index{agg}: None'.format(agg=' (aggregated)' if self._aggregations else '')
        else:
            s += '\n  {ind}{agg}:\n    {index_lines}'.format(ind=plural('Index', len(indices.axes), 'Indices'),
                                                             agg=' (aggregated)' if self._aggregations else '',
                                                             index_lines='\n    '.join('{} of {}'.format(
                                                                 axis, indices.source) for axis in indices.axes))
        if self._joins:
            s += '\n  Dependent on {} {}'.format(len(self._joins),
                                                 plural('broadcast/join', len(self._joins), 'broadcasts/joins'))
        return s

    def __lt__(self, other):
        raise NotImplementedError("'<' comparison with expression of type {}".format(str(self._type)))

    def __le__(self, other):
        raise NotImplementedError("'<=' comparison with expression of type {}".format(str(self._type)))

    def __gt__(self, other):
        raise NotImplementedError("'>' comparison with expression of type {}".format(str(self._type)))

    def __ge__(self, other):
        raise NotImplementedError("'>=' comparison with expression of type {}".format(str(self._type)))

    def _init(self):
        pass

    def __nonzero__(self):
        raise NotImplementedError(
            "The truth value of an expression is undefined\n"
            "    Hint: instead of 'if x', use 'hl.cond(x, ...)'\n"
            "    Hint: instead of 'x and y' or 'x or y', use 'x & y' or 'x | y'\n"
            "    Hint: instead of 'not x', use '~x'")

    def __iter__(self):
        raise TypeError("'Expression' object is not iterable")

    def _unary_op(self, name):
        return construct_expr(UnaryOperation(self._ast, name),
                              self._type, self._indices, self._aggregations, self._joins, self._refs)

    def _bin_op(self, name, other, ret_type):
        other = to_expr(other)
        indices, aggregations, joins, refs = unify_all(self, other)
        return construct_expr(BinaryOperation(self._ast, other._ast, name), ret_type, indices, aggregations, joins,
                              refs)

    def _bin_op_reverse(self, name, other, ret_type):
        other = to_expr(other)
        indices, aggregations, joins, refs = unify_all(self, other)
        return construct_expr(BinaryOperation(other._ast, self._ast, name), ret_type, indices, aggregations, joins,
                              refs)

    def _field(self, name, ret_type):
        return construct_expr(Select(self._ast, name), ret_type, self._indices,
                              self._aggregations, self._joins, self._refs)

    def _method(self, name, ret_type, *args):
        args = tuple(to_expr(arg) for arg in args)
        indices, aggregations, joins, refs = unify_all(self, *args)
        return construct_expr(ClassMethod(name, self._ast, *(a._ast for a in args)),
                              ret_type, indices, aggregations, joins, refs)

    def _index(self, ret_type, key):
        key = to_expr(key)
        indices, aggregations, joins, refs = unify_all(self, key)
        return construct_expr(Index(self._ast, key._ast),
                              ret_type, indices, aggregations, joins, refs)

    def _slice(self, ret_type, start=None, stop=None, step=None):
        if start is not None:
            start = to_expr(start)
            start_ast = start._ast
        else:
            start_ast = None
        if stop is not None:
            stop = to_expr(stop)
            stop_ast = stop._ast
        else:
            stop_ast = None
        if step is not None:
            raise NotImplementedError('Variable slice step size is not currently supported')

        non_null = [x for x in [start, stop] if x is not None]
        indices, aggregations, joins, refs = unify_all(self, *non_null)
        return construct_expr(Index(self._ast, Slice(start_ast, stop_ast)),
                              ret_type, indices, aggregations, joins, refs)

    def _bin_lambda_method(self, name, f, input_type, ret_type_f, *args):
        args = (to_expr(arg) for arg in args)
        new_id = Env._get_uid()
        lambda_result = to_expr(
            f(construct_expr(Reference(new_id), input_type, self._indices, self._aggregations, self._joins,
                             self._refs)))
        indices, aggregations, joins, refs = unify_all(self, lambda_result)
        ast = LambdaClassMethod(name, new_id, self._ast, lambda_result._ast, *(a._ast for a in args))
        return construct_expr(ast, ret_type_f(lambda_result._type), indices, aggregations, joins, refs)

    @property
    def dtype(self):
        """The data type of the expression.

        Returns
        -------
        :class:`.Type`
            Data type.
        """
        return self._type

    def __eq__(self, other):
        """Returns ``True`` if the two expressions are equal.

        Examples
        --------

        .. doctest::

            >>> x = hl.capture(5)
            >>> y = hl.capture(5)
            >>> z = hl.capture(1)

            >>> hl.eval_expr(x == y)
            True

            >>> hl.eval_expr(x == z)
            False

        Notes
        -----
        This method will fail with an error if the two expressions are not
        of comparable types.

        Parameters
        ----------
        other : :class:`.Expression`
            Expression for equality comparison.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the two expressions are equal.
        """
        other = to_expr(other)
        t = unify_types(self._type, other._type)
        if t is None:
            raise TypeError("Invalid '==' comparison, cannot compare expressions of type '{}' and '{}'".format(
                self._type, other._type
            ))
        return self._bin_op("==", other, TBoolean())

    def __ne__(self, other):
        """Returns ``True`` if the two expressions are not equal.

        Examples
        --------

        .. doctest::

            >>> x = hl.capture(5)
            >>> y = hl.capture(5)
            >>> z = hl.capture(1)

            >>> hl.eval_expr(x != y)
            False

            >>> hl.eval_expr(x != z)
            True

        Notes
        -----
        This method will fail with an error if the two expressions are not
        of comparable types.

        Parameters
        ----------
        other : :class:`.Expression`
            Expression for inequality comparison.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the two expressions are not equal.
        """
        other = to_expr(other)
        t = unify_types(self._type, other._type)
        if t is None:
            raise TypeError("Invalid '!=' comparison, cannot compare expressions of type '{}' and '{}'".format(
                self._type, other._type
            ))
        return self._bin_op("!=", other, TBoolean())


class CollectionExpression(Expression):
    """Expression of type :class:`.TArray` or :class:`.TSet`

    >>> a = hl.capture([1, 2, 3, 4, 5])

    >>> s = hl.capture({'Alice', 'Bob', 'Charlie'})
    """

    @typecheck_method(f=func_spec(1, expr_bool))
    def exists(self, f):
        """Returns ``True`` if `f` returns ``True`` for any element.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.exists(lambda x: x % 2 == 0))
            True

            >>> hl.eval_expr(s.exists(lambda x: x[0] == 'D'))
            False

        Notes
        -----
        This method always returns ``False`` for empty collections.

        Parameters
        ----------
        f : function ( (arg) -> :class:`.BooleanExpression`)
            Function to evaluate for each element of the collection. Must return a
            :class:`.BooleanExpression`.

        Returns
        -------
        :class:`.BooleanExpression`.
            ``True`` if `f` returns ``True`` for any element, ``False`` otherwise.
        """

        def unify_ret(t):
            if not isinstance(t, TBoolean):
                raise TypeError("'exists' expects 'f' to return an expression of type 'Boolean', found '{}'".format(t))
            return t

        return self._bin_lambda_method("exists", f, self._type.element_type, unify_ret)

    @typecheck_method(f=func_spec(1, expr_bool))
    def filter(self, f):
        """Returns a new collection containing elements where `f` returns ``True``.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.filter(lambda x: x % 2 == 0))
            [2, 4]

            >>> hl.eval_expr(s.filter(lambda x: ~(x[-1] == 'e')))
            {'Bob'}

        Notes
        -----
        Returns a same-type expression; evaluated on a :class:`.SetExpression`, returns a
        :class:`.SetExpression`. Evaluated on an :class:`.ArrayExpression`,
        returns an :class:`.ArrayExpression`.

        Parameters
        ----------
        f : function ( (arg) -> :class:`.BooleanExpression`)
            Function to evaluate for each element of the collection. Must return a
            :class:`.BooleanExpression`.

        Returns
        -------
        :class:`.CollectionExpression`
            Expression of the same type as the callee.
        """

        def unify_ret(t):
            if not isinstance(t, TBoolean):
                raise TypeError("'filter' expects 'f' to return an expression of type 'Boolean', found '{}'".format(t))
            return self._type

        return self._bin_lambda_method("filter", f, self._type.element_type, unify_ret)

    @typecheck_method(f=func_spec(1, expr_bool))
    def find(self, f):
        """Returns the first element where `f` returns ``True``.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.find(lambda x: x ** 2 > 20))
            5

            >>> hl.eval_expr(s.find(lambda x: x[0] == 'D'))
            None

        Notes
        -----
        If `f` returns ``False`` for every element, then the result is missing.

        Parameters
        ----------
        f : function ( (arg) -> :class:`.BooleanExpression`)
            Function to evaluate for each element of the collection. Must return a
            :class:`.BooleanExpression`.

        Returns
        -------
        :class:`.Expression`
            Expression whose type is the element type of the collection.
        """

        def unify_ret(t):
            if not isinstance(t, TBoolean):
                raise TypeError("'find' expects 'f' to return an expression of type 'Boolean', found '{}'".format(t))
            return self._type.element_type

        return self._bin_lambda_method("find", f, self._type.element_type, unify_ret)

    @typecheck_method(f=func_spec(1, expr_any))
    def flatmap(self, f):
        """Map each element of the collection to a new collection, and flatten the results.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.flatmap(lambda x: hl.range(0, x)))
            [0, 0, 1, 0, 1, 2, 0, 1, 2, 3, 0, 1, 2, 3, 4]

            >>> hl.eval_expr(s.flatmap(lambda x: hl.range(0, x.length()).map(lambda i: x[i]).to_set()))
            {'A', 'B', 'C', 'a', 'b', 'c', 'e', 'h', 'i', 'l', 'o', 'r'}

        Parameters
        ----------
        f : function ( (arg) -> :class:`.CollectionExpression`)
            Function from the element type of the collection to the type of the
            collection. For instance, `flatmap` on a ``Set[String]`` should take
            a ``String`` and return a ``Set``.

        Returns
        -------
        :class:`.CollectionExpression`
        """
        expected_type, s = (TArray, 'Array') if isinstance(self._type, TArray) else (TSet, 'Set')

        def unify_ret(t):
            if not isinstance(t, expected_type):
                raise TypeError("'flatmap' expects 'f' to return an expression of type '{}', found '{}'".format(s, t))
            return t

        return self._bin_lambda_method("flatMap", f, self._type.element_type, unify_ret)

    @typecheck_method(f=func_spec(1, expr_bool))
    def forall(self, f):
        """Returns ``True`` if `f` returns ``True`` for every element.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.forall(lambda x: x < 10))
            True

        Notes
        -----
        This method returns ``True`` if the collection is empty.

        Parameters
        ----------
        f : function ( (arg) -> :class:`.BooleanExpression`)
            Function to evaluate for each element of the collection. Must return a
            :class:`.BooleanExpression`.

        Returns
        -------
        :class:`.BooleanExpression`.
            ``True`` if `f` returns ``True`` for every element, ``False`` otherwise.
        """

        def unify_ret(t):
            if not isinstance(t, TBoolean):
                raise TypeError("'forall' expects 'f' to return an expression of type 'Boolean', found '{}'".format(t))
            return t

        return self._bin_lambda_method("forall", f, self._type.element_type, unify_ret)

    @typecheck_method(f=func_spec(1, expr_any))
    def group_by(self, f):
        """Group elements into a dict according to a lambda function.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.group_by(lambda x: x % 2 == 0))
            {False: [1, 3, 5], True: [2, 4]}

            >>> hl.eval_expr(s.group_by(lambda x: x.length()))
            {3: {'Bob'}, 5: {'Alice'}, 7: {'Charlie'}}

        Parameters
        ----------
        f : function ( (arg) -> :class:`.Expression`)
            Function to evaluate for each element of the collection to produce a key for the
            resulting dictionary.

        Returns
        -------
        :class:`.DictExpression`.
            Dictionary keyed by results of `f`.
        """
        return self._bin_lambda_method("groupBy", f, self._type.element_type, lambda t: TDict(t, self._type))

    @typecheck_method(f=func_spec(1, expr_any))
    def map(self, f):
        """Transform each element of a collection.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.map(lambda x: x ** 3))
            [1.0, 8.0, 27.0, 64.0, 125.0]

            >>> hl.eval_expr(s.map(lambda x: x.length()))
            {3, 5, 7}

        Parameters
        ----------
        f : function ( (arg) -> :class:`.Expression`)
            Function to transform each element of the collection.

        Returns
        -------
        :class:`.CollectionExpression`.
            Collection where each element has been transformed according to `f`.
        """
        return self._bin_lambda_method("map", f, self._type.element_type, lambda t: self._type.__class__(t))

    def length(self):
        """Returns the size of a collection.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.length())
            5

            >>> hl.eval_expr(s.length())
            3

        Returns
        -------
        :class:`.Int32Expression`
            The number of elements in the collection.
        """
        return self._method("size", TInt32())

    def size(self):
        """Returns the size of a collection.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.size())
            5

            >>> hl.eval_expr(s.size())
            3

        Returns
        -------
        :class:`.Int32Expression`
            The number of elements in the collection.
        """
        return self._method("size", TInt32())


class CollectionNumericExpression(CollectionExpression):
    """Expression of type :class:`.TArray` or :class:`.TSet` with numeric element type.

    >>> a = hl.capture([1, 2, 3, 4, 5])
    """

    def max(self):
        """Returns the maximum element.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.max())
            5

        Returns
        -------
        :class:`.NumericExpression`
            The maximum value in the collection.
        """
        return self._method("max", self._type.element_type)

    def min(self):
        """Returns the minimum element.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.min())
            1

        Returns
        -------
        :class:`.NumericExpression`
            The miniumum value in the collection.
        """
        return self._method("min", self._type.element_type)

    def mean(self):
        """Returns the mean of all values in the collection.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.mean())
            3.0

        Note
        ----
        Missing elements are ignored.

        Returns
        -------
        :class:`.Float64Expression`
            The mean value of the collection.
        """
        return self._method("mean", TFloat64())

    def median(self):
        """Returns the median element.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.median())
            3

        Note
        ----
        Missing elements are ignored.

        Returns
        -------
        :class:`.NumericExpression`
            The median value in the collection.
        """
        return self._method("median", self._type.element_type)

    def product(self):
        """Returns the product of all elements in the collection.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.product())
            120

        Note
        ----
        Missing elements are ignored.

        Returns
        -------
        :class:`.NumericExpression`
            The product of the collection.
        """
        return self._method("product", self._type.element_type)

    def sum(self):
        """Returns the sum of all elements in the collection.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.sum())
            15

        Note
        ----
        Missing elements are ignored.

        Returns
        -------
        :class:`.NumericExpression`
            The sum of the collection.
        """
        return self._method("sum", self._type.element_type)


class ArrayExpression(CollectionExpression):
    """Expression of type :class:`.TArray`.

    >>> a = hl.capture(['Alice', 'Bob', 'Charlie'])
    """

    def __getitem__(self, item):
        """Index into or slice the array.

        Examples
        --------

        Index with a single integer:

        .. doctest::

            >>> hl.eval_expr(a[1])
            'Bob'

            >>> hl.eval_expr(a[-1])
            'Charlie'

        Slicing is also supported:

        .. doctest::

            >>> hl.eval_expr(a[1:])
            ['Bob', 'Charlie']

        Parameters
        ----------
        item : slice or :class:`.Int32Expression`
            Index or slice.

        Returns
        -------
        :class:`.Expression`
            Element or array slice.
        """
        if isinstance(item, slice):
            return self._slice(self._type, item.start, item.stop, item.step)
        else:
            item = to_expr(item)
            if not isinstance(item._type, TInt32):
                raise TypeError("Array expects key to be type 'slice' or expression of type 'Int32', "
                                "found expression of type '{}'".format(item._type))
            return self._index(self._type.element_type, item)

    @typecheck_method(item=expr_any)
    def contains(self, item):
        """Returns a boolean indicating whether `item` is found in the array.

        Examples
        --------

        .. doctest::

            >>> hl.eval_expr(a.contains('Charlie'))
            True

            >>> hl.eval_expr(a.contains('Helen'))
            False

        Parameters
        ----------
        item : :class:`.Expression`
            Item for inclusion test.

        Warning
        -------
        This method takes time proportional to the length of the array. If a
        pipeline uses this method on the same array several times, it may be
        more efficient to convert the array to a set first
        (:meth:`.ArrayExpression.to_set`).

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the element is found in the array, ``False`` otherwise.
        """
        return self.exists(lambda x: x == item)

    @typecheck_method(item=expr_any)
    def append(self, item):
        """Append an element to the array and return the result.

        Examples
        --------

        .. doctest::

            >>> hl.eval_expr(a.append('Dan'))
            ['Alice', 'Bob', 'Charlie', 'Dan']

        Parameters
        ----------
        item : :class:`.Expression`
            Element to append, same type as the array element type.

        Returns
        -------
        :class:`.ArrayExpression`
        """
        if not item._type == self._type.element_type:
            raise TypeError("'ArrayExpression.append' expects 'item' to be the same type as its elements\n"
                            "    array element type: '{}'\n"
                            "    type of arg 'item': '{}'".format(self._type._element_type, item._type))
        return self._method("append", self._type, item)

    @typecheck_method(a=expr_list)
    def extend(self, a):
        """Concatenate two arrays and return the result.

        Examples
        --------

        .. doctest::

            >>> hl.eval_expr(a.extend(['Dan', 'Edith']))
            ['Alice', 'Bob', 'Charlie', 'Dan', 'Edith']

        Parameters
        ----------
        a : :class:`.ArrayExpression`
            Array to concatenate, same type as the callee.

        Returns
        -------
        :class:`.ArrayExpression`
        """
        if not a._type == self._type:
            raise TypeError("'ArrayExpression.extend' expects 'a' to be the same type as the caller\n"
                            "    caller type: '{}'\n"
                            "    type of 'a': '{}'".format(self._type, a._type))
        return self._method("extend", self._type, a)

    @typecheck_method(f=func_spec(1, expr_any), ascending=expr_bool)
    def sort_by(self, f, ascending=True):
        """Sort the array according to a function.

        Examples
        --------

        .. doctest::

            >>> hl.eval_expr(a.sort_by(lambda x: x, ascending=False))
            ['Charlie', 'Bob', 'Alice']

        Parameters
        ----------
        f : function ( (arg) -> :class:`.BooleanExpression`)
            Function to evaluate per element to obtain the sort key.
        ascending : bool or :class:`.BooleanExpression`
            Sort in ascending order.

        Returns
        -------
        :class:`.ArrayExpression`
        """

        def check_f(t):
            if not (is_numeric(t) or isinstance(t, TString)):
                raise TypeError("'sort_by' expects 'f' to return an ordered type, found type '{}'\n"
                                "    Ordered types are 'Int32', 'Int64', 'Float32', 'Float64', 'String'".format(t))
            return self._type

        return self._bin_lambda_method("sortBy", f, self._type.element_type, check_f, ascending)

    def to_set(self):
        """Convert the array to a set.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.to_set())
            {'Alice', 'Bob', 'Charlie'}

        Returns
        -------
        :class:`.SetExpression`
            Set of all unique elements.
        """
        return self._method("toSet", TSet(self._type.element_type))

class ArrayNumericExpression(ArrayExpression, CollectionNumericExpression):
    """Expression of type :class:`.TArray` with a numeric type.

    Numeric arrays support arithmetic both with scalar values and other arrays.
    Arithmetic between two numeric arrays requires that the length of each array
    is identical, and will apply the operation positionally (``a1 * a2`` will
    multiply the first element of ``a1`` by the first element of ``a2``, the
    second element of ``a1`` by the second element of ``a2``, and so on).
    Arithmetic with a scalar will apply the operation to each element of the
    array.

    >>> a1 = hl.capture([0, 1, 2, 3, 4, 5])

    >>> a2 = hl.capture([1, -1, 1, -1, 1, -1])

    """

    def _bin_op_ret_typ(self, other):
        if isinstance(other._type, TArray):
            t = other._type.element_type
        else:
            t = other._type
        t = unify_types(self._type.element_type, t)
        if not t:
            return None
        else:
            return TArray(t)

    def _bin_op_numeric(self, name, other, ret_type_f=None):
        other = to_expr(other)
        ret_type = self._bin_op_ret_typ(other)
        if not ret_type:
            raise NotImplementedError("'{}' {} '{}'".format(
                self._type, name, other._type))
        if ret_type_f:
            ret_type = ret_type_f(ret_type)
        return self._bin_op(name, other, ret_type)

    def _bin_op_numeric_reverse(self, name, other, ret_type_f=None):
        other = to_expr(other)
        ret_type = self._bin_op_ret_typ(other)
        if not ret_type:
            raise NotImplementedError("'{}' {} '{}'".format(
                other._type, name, self._type))
        if ret_type_f:
            ret_type = ret_type_f(ret_type)
        return self._bin_op_reverse(name, other, ret_type)

    def __add__(self, other):
        """Positionally add an array or a scalar.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a1 + 5)
            [5, 6, 7, 8, 9, 10]

            >>> hl.eval_expr(a1 + a2)
            [1, 0, 3, 2, 5, 4]

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.ArrayNumericExpression`
            Value or array to add.

        Returns
        -------
        :class:`.ArrayNumericExpression`
            Array of positional sums.
        """
        return self._bin_op_numeric("+", other)

    def __radd__(self, other):
        return self._bin_op_numeric_reverse("+", other)

    def __sub__(self, other):
        """Positionally subtract an array or a scalar.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a2 - 1)
            [0, -2, 0, -2, 0, -2]

            >>> hl.eval_expr(a1 - a2)
            [-1, 2, 1, 4, 3, 6]

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.ArrayNumericExpression`
            Value or array to subtract.

        Returns
        -------
        :class:`.ArrayNumericExpression`
            Array of positional differences.
        """
        return self._bin_op_numeric("-", other)

    def __rsub__(self, other):
        return self._bin_op_numeric_reverse("-", other)

    def __mul__(self, other):
        """Positionally multiply by an array or a scalar.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a2 * 5)
            [5, -5, 5, -5, 5, -5]

            >>> hl.eval_expr(a1 * a2)
            [0, -1, 2, -3, 4, -5]

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.ArrayNumericExpression`
            Value or array to multiply by.

        Returns
        -------
        :class:`.ArrayNumericExpression`
            Array of positional products.
        """
        return self._bin_op_numeric("*", other)

    def __rmul__(self, other):
        return self._bin_op_numeric_reverse("*", other)

    def __div__(self, other):
        """Positionally divide by an array or a scalar.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a1 / 10)
            [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]

            >>> hl.eval_expr(a2 / a1)
            [inf, -1.0, 0.5, -0.3333333333333333, 0.25, -0.2]

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.ArrayNumericExpression`
            Value or array to divide by.

        Returns
        -------
        :class:`.ArrayNumericExpression`
            Array of positional quotients.
        """

        def ret_type_f(t):
            assert isinstance(t, TArray)
            assert is_numeric(t.element_type)
            if isinstance(t.element_type, TInt32) or isinstance(t.element_type, TInt64):
                return TArray(TFloat32())
            else:
                # Float64 or Float32
                return t

        return self._bin_op_numeric("/", other, ret_type_f)

    def __rdiv__(self, other):
        def ret_type_f(t):
            assert isinstance(t, TArray)
            assert is_numeric(t.element_type)
            if isinstance(t.element_type, TInt32) or isinstance(t.element_type, TInt64):
                return TArray(TFloat32())
            else:
                # Float64 or Float32
                return t

        return self._bin_op_numeric_reverse("/", other, ret_type_f)

    def __floordiv__(self, other):
        """Positionally divide by an array or a scalar using floor division.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a1 // 2)
            [0, 0, 1, 1, 2, 2]

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.ArrayNumericExpression`

        Returns
        -------
        :class:`.ArrayNumericExpression`
        """
        return self._bin_op_numeric('//', other)

    def __rfloordiv__(self, other):
        return self._bin_op_numeric_reverse('//', other)

    def __mod__(self, other):
        """Positionally compute the left modulo the right.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a1 % 2)
            [0, 1, 0, 1, 0, 1]

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.ArrayNumericExpression`

        Returns
        -------
        :class:`.ArrayNumericExpression`
        """
        return self._bin_op_numeric('%', other)

    def __rmod__(self, other):
        return self._bin_op_numeric_reverse('%', other)

    def __pow__(self, other):
        """Positionally raise to the power of an array or a scalar.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a1 ** 2)
            [0.0, 1.0, 4.0, 9.0, 16.0, 25.0]

            >>> hl.eval_expr(a1 ** a2)
            [0.0, 1.0, 2.0, 0.3333333333333333, 4.0, 0.2]

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.ArrayNumericExpression`

        Returns
        -------
        :class:`.ArrayNumericExpression`
        """
        return self._bin_op_numeric('**', other, lambda _: TArray(TFloat64()))

    def __rpow__(self, other):
        return self._bin_op_numeric_reverse('**', other, lambda _: TArray(TFloat64()))

    @typecheck_method(ascending=expr_bool)
    def sort(self, ascending=True):
        """Returns a sorted array.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a1.sort())
            [-1, -1, -1, 1, 1, 1]

        Parameters
        ----------
        ascending : :class:`.BooleanExpression`
            Sort in ascending order.

        Returns
        -------
        :class:`.ArrayNumericExpression`
            Sorted array.
        """
        return self._method("sort", self._type, ascending)

    def unique_min_index(self):
        """Return the index of the minimum value in the array.

        Notes
        -----
        If the minimum value is not unique, returns missing.

        Examples
        --------

        .. doctest::

            >>> pl = hl.capture([10, 0, 100])
            [10, 0, 100]

            >>> hl.eval_expr(pl.unique_min_index())
            1

            >>> pl2 = hl.capture([0, 0, 100])
            [0, 0, 100]

            >>> hl.eval_expr(pl2.unique_min_index())
            None

        Returns
        -------
        :class:`.Int32Expression`
        """
        return self._method("uniqueMinIndex", TInt32())

    def unique_max_index(self):
        """Return the index of the maximum value in the array.

        Notes
        -----
        If the maximum value is not unique, returns missing.

        Examples
        --------

        .. doctest::

            >>> gp = hl.capture([0.2, 0.2, 0.6])
            [0.2, 0.2, 0.6]

            >>> hl.eval_expr(gp.unique_max_index())
            2

            >>> gp2 = hl.capture([0.4, 0.4, 0.2])
            [0.4, 0.4, 0.2]

            >>> hl.eval_expr(gp2.unique_max_index())
            None

        Returns
        -------
        :class:`.Int32Expression`
        """
        return self._method("uniqueMinIndex", TInt32())


class ArrayBooleanExpression(ArrayExpression):
    """Expression of type :class:`.TArray` with element type :class:`.TBoolean`."""
    pass


class ArrayFloat64Expression(ArrayNumericExpression):
    """Expression of type :class:`.TArray` with element type :class:`.TFloat64`."""
    pass


class ArrayFloat32Expression(ArrayNumericExpression):
    """Expression of type :class:`.TArray` with element type :class:`.TFloat32`."""
    pass


class ArrayInt32Expression(ArrayNumericExpression):
    """Expression of type :class:`.TArray` with element type :class:`.TInt32`"""
    pass


class ArrayInt64Expression(ArrayNumericExpression):
    """Expression of type :class:`.TArray` with element type :class:`.TInt64`."""
    pass


class ArrayStringExpression(ArrayExpression):
    """Expression of type :class:`.TArray` with element type :class:`.TString`.

    >>> a = hl.capture(['Alice', 'Bob', 'Charles'])
    """

    @typecheck_method(delimiter=expr_str)
    def mkstring(self, delimiter):
        """Joins the elements of the array into a single string delimited by `delimiter`.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.mkstring(','))
            'Alice,Bob,Charles'

        Parameters
        ----------
        delimiter : :class:`.StringExpression`
            Field delimiter.

        Returns
        -------
        :class:`.StringExpression`
            Joined string expression.
        """
        return self._method("mkString", TString(), delimiter)

    @typecheck_method(ascending=expr_bool)
    def sort(self, ascending=True):
        """Returns a sorted array.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.sort(ascending=False))
            ['Charles', 'Bob', 'Alice']

        Parameters
        ----------
        ascending : bool or :class:`.BooleanExpression`
            Sort in ascending order.

        Returns
        -------
        :class:`.ArrayStringExpression`
            Sorted array.
        """
        return self._method("sort", self._type, ascending)


class ArrayStructExpression(ArrayExpression):
    """Expression of type :class:`.TArray` with element type :class:`.TStruct`."""
    pass


class ArrayArrayExpression(ArrayExpression):
    """Expression of type :class:`.TArray` with element type :class:`.TArray`.

    >>> a = hl.capture([[1, 2], [3, 4]])
    """

    def flatten(self):
        """Flatten the nested array by concatenating subarrays.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(a.flatten())
            [1, 2, 3, 4]

        Returns
        -------
        :class:`.ArrayExpression`
            Array generated by concatenating all subarrays.
        """
        return self._method("flatten", self._type.element_type)


class SetExpression(CollectionExpression):
    """Expression of type :class:`.TSet`.

    >>> s1 = hl.capture({1, 2, 3})
    >>> s2 = hl.capture({1, 3, 5})
    """

    @typecheck_method(item=expr_any)
    def add(self, item):
        """Returns a new set including `item`.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s1.add(10))
            {1, 2, 3, 10}

        Parameters
        ----------
        item : :class:`.Expression`
            Value to add.

        Returns
        -------
        :class:`.SetExpression`
            Set with `item` added.
        """
        if not item._type == self._type.element_type:
            raise TypeError("'SetExpression.add' expects 'item' to be the same type as its elements\n"
                            "    set element type:   '{}'\n"
                            "    type of arg 'item': '{}'".format(self._type._element_type, item._type))
        return self._method("add", self._type, item)

    @typecheck_method(item=expr_any)
    def remove(self, item):
        """Returns a new set excluding `item`.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s1.remove(1))
            {2, 3}

        Parameters
        ----------
        item : :class:`.Expression`
            Value to remove.

        Returns
        -------
        :class:`.SetExpression`
            Set with `item` removed.
        """
        if not item._type == self._type.element_type:
            raise TypeError("'SetExpression.remove' expects 'item' to be the same type as its elements\n"
                            "    set element type:   '{}'\n"
                            "    type of arg 'item': '{}'".format(self._type._element_type, item._type))
        return self._method("remove", self._type, item)

    @typecheck_method(item=expr_any)
    def contains(self, item):
        """Returns ``True`` if `item` is in the set.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s1.contains(1))
            True

            >>> hl.eval_expr(s1.contains(10))
            False

        Parameters
        ----------
        item : :class:`.Expression`
            Value for inclusion test..

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if `item` is in the set.
        """
        if not item._type == self._type.element_type:
            raise TypeError("'SetExpression.contains' expects 'item' to be the same type as its elements\n"
                            "    set element type:   '{}'\n"
                            "    type of arg 'item': '{}'".format(self._type._element_type, item._type))
        return self._method("contains", TBoolean(), item)

    @typecheck_method(s=expr_set)
    def difference(self, s):
        """Return the set of elements in the set that are not present in set `s`.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s1.difference(s2))
            {2}

            >>> hl.eval_expr(s2.difference(s1))
            {5}

        Parameters
        ----------
        s : :class:`.SetExpression`
            Set expression of the same type.

        Returns
        -------
        :class:`.SetExpression`
            Set of elements not in `s`.
        """
        if not s._type.element_type == self._type.element_type:
            raise TypeError("'SetExpression.difference' expects 's' to be the same type\n"
                            "    set type:    '{}'\n"
                            "    type of 's': '{}'".format(self._type, s._type))
        return self._method("difference", self._type, s)

    @typecheck_method(s=expr_set)
    def intersection(self, s):
        """Return the intersection of the set and set `s`.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s1.intersection(s2))
            {1, 3}

        Parameters
        ----------
        s : :class:`.SetExpression`
            Set expression of the same type.

        Returns
        -------
        :class:`.SetExpression`
            Set of elements present in `s`.
        """
        if not s._type.element_type == self._type.element_type:
            raise TypeError("'SetExpression.intersection' expects 's' to be the same type\n"
                            "    set type:    '{}'\n"
                            "    type of 's': '{}'".format(self._type, s._type))
        return self._method("intersection", self._type, s)

    @typecheck_method(s=expr_set)
    def is_subset(self, s):
        """Returns ``True`` if every element is contained in set `s`.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s1.is_subset(s2))
            False

            >>> hl.eval_expr(s1.remove(2).is_subset(s2))
            True

        Parameters
        ----------
        s : :class:`.SetExpression`
            Set expression of the same type.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if every element is contained in set `s`.
        """
        if not s._type.element_type == self._type.element_type:
            raise TypeError("'SetExpression.is_subset' expects 's' to be the same type\n"
                            "    set type:    '{}'\n"
                            "    type of 's': '{}'".format(self._type, s._type))
        return self._method("isSubset", TBoolean(), s)

    @typecheck_method(s=expr_set)
    def union(self, s):
        """Return the union of the set and set `s`.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s1.union(s2))
            {1, 2, 3, 5}

        Parameters
        ----------
        s : :class:`.SetExpression`
            Set expression of the same type.

        Returns
        -------
        :class:`.SetExpression`
            Set of elements present in either set.
        """
        if not s._type.element_type == self._type.element_type:
            raise TypeError("'SetExpression.union' expects 's' to be the same type\n"
                            "    set type:    '{}'\n"
                            "    type of 's': '{}'".format(self._type, s._type))
        return self._method("union", self._type, s)

    def to_array(self):
        """Convert the set to an array.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s1.to_array())
            [1, 2, 3]

        Notes
        -----
        The order of elements in the array is not guaranteed.

        Returns
        -------
        :class:`.ArrayExpression`
            Array of all elements in the set.
        """
        return self._method("toArray", TArray(self._type.element_type))


class SetFloat64Expression(SetExpression, CollectionNumericExpression):
    """Expression of type :class:`.TSet` with element type :class:`.TFloat64`."""
    pass


class SetFloat32Expression(SetExpression, CollectionNumericExpression):
    """Expression of type :class:`.TSet` with element type :class:`.TFloat32`."""
    pass


class SetInt32Expression(SetExpression, CollectionNumericExpression):
    """Expression of type :class:`.TSet` with element type :class:`.TInt32`."""
    pass


class SetInt64Expression(SetExpression, CollectionNumericExpression):
    """Expression of type :class:`.TSet` with element type :class:`.TInt64`."""
    pass


class SetStringExpression(SetExpression):
    """Expression of type :class:`.TSet` with element type :class:`.TString`.

    >>> s = hl.capture({'Alice', 'Bob', 'Charles'})
    """

    @typecheck_method(delimiter=expr_str)
    def mkstring(self, delimiter):
        """Joins the elements of the set into a single string delimited by `delimiter`.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s.mkstring(','))
            'Bob,Charles,Alice'

        Notes
        -----
        The order of the elements in the string is not guaranteed.

        Parameters
        ----------
        delimiter : str or :class:`.StringExpression`
            Field delimiter.

        Returns
        -------
        :class:`.StringExpression`
            Joined string expression.
        """
        return self._method("mkString", TString(), delimiter)


class SetSetExpression(SetExpression):
    """Expression of type :class:`.TSet` with element type :class:`.TSet`.

    >>> s = hl.capture({1, 2, 3}).map(lambda s: {s, 2 * s})
    """

    def flatten(self):
        """Flatten the nested set by concatenating subsets.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s.flatten())
            {1, 2, 3, 4, 6}

        Returns
        -------
        :class:`.SetExpression`
            Set generated by concatenating all subsets.
        """
        return self._method("flatten", self._type.element_type)


class DictExpression(Expression):
    """Expression of type :class:`.TDict`.

    >>> d = hl.capture({'Alice': 43, 'Bob': 33, 'Charles': 44})
    """

    def _init(self):
        self._key_typ = self._type.key_type
        self._value_typ = self._type.value_type

    @typecheck_method(item=expr_any)
    def __getitem__(self, item):
        """Get the value associated with key `item`.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(d['Alice'])
            43

        Notes
        -----
        Raises an error if `item` is not a key of the dictionary. Use
        :meth:`.DictExpression.get` to return missing instead of an error.

        Parameters
        ----------
        item : :class:`.Expression`
            Key expression.

        Returns
        -------
        :class:`.Expression`
            Value associated with key `item`.
        """
        if not item._type == self._type.key_type:
            raise TypeError("dict encountered an invalid key type\n"
                            "    dict key type:  '{}'\n"
                            "    type of 'item': '{}'".format(self._type.key_type, item._type))
        return self._index(self._value_typ, item)

    @typecheck_method(item=expr_any)
    def contains(self, item):
        """Returns whether a given key is present in the dictionary.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(d.contains('Alice'))
            True

            >>> hl.eval_expr(d.contains('Anne'))
            False

        Parameters
        ----------
        item : :class:`.Expression`
            Key to test for inclusion.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if `item` is a key of the dictionary, ``False`` otherwise.
        """
        if not item._type == self._type.key_type:
            raise TypeError("'DictExpression.contains' encountered an invalid key type\n"
                            "    dict key type:  '{}'\n"
                            "    type of 'item': '{}'".format(self._type.key_type, item._type))
        return self._method("contains", TBoolean(), item)

    @typecheck_method(item=expr_any)
    def get(self, item):
        """Returns the value associated with key `k`, or missing if that key is not present.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(d.get('Alice'))
            43

            >>> hl.eval_expr(d.get('Anne'))
            None

        Parameters
        ----------
        item : :class:`.Expression`
            Key.

        Returns
        -------
        :class:`.Expression`
            The value associated with `item`, or missing.
        """
        if not item._type == self._type.key_type:
            raise TypeError("'DictExpression.get' encountered an invalid key type\n"
                            "    dict key type:  '{}'\n"
                            "    type of 'item': '{}'".format(self._type.key_type, item._type))
        return self._method("get", self._value_typ, item)

    def key_set(self):
        """Returns the set of keys in the dictionary.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(d.key_set())
            {'Alice', 'Bob', 'Charles'}

        Returns
        -------
        :class:`.SetExpression`
            Set of all keys.
        """
        return self._method("keySet", TSet(self._key_typ))

    def keys(self):
        """Returns an array with all keys in the dictionary.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(d.keys())
            ['Bob', 'Charles', 'Alice']

        Returns
        -------
        :class:`.ArrayExpression`
            Array of all keys.
        """
        return self._method("keys", TArray(self._key_typ))

    @typecheck_method(f=func_spec(1, expr_any))
    def map_values(self, f):
        """Transform values of the dictionary according to a function.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(d.map_values(lambda x: x * 10))
            {'Alice': 430, 'Bob': 330, 'Charles': 440}

        Parameters
        ----------
        f : function ( (arg) -> :class:`.Expression`)
            Function to apply to each value.

        Returns
        -------
        :class:`.DictExpression`
            Dictionary with transformed values.
        """
        return self._bin_lambda_method("mapValues", f, self._value_typ, lambda t: TDict(self._key_typ, t))

    def size(self):
        """Returns the size of the dictionary.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(d.size())
            3

        Returns
        -------
        :class:`.Int32Expression`
            Size of the dictionary.
        """
        return self._method("size", TInt32())

    def values(self):
        """Returns an array with all values in the dictionary.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(d.values())
            [33, 44, 43]

        Returns
        -------
        :class:`.ArrayExpression`
            All values in the dictionary.
        """
        return self._method("values", TArray(self._value_typ))


class Aggregable(object):
    """Expression that can only be aggregated.

    An :class:`.Aggregable` is produced by the :meth:`.explode` or :meth:`.filter`
    methods. These objects can be aggregated using aggregator functions, but
    cannot otherwise be used in expressions.
    """

    def __init__(self, ast, type, indices, aggregations, joins, refs):
        self._ast = ast
        self._type = type
        self._indices = indices
        self._aggregations = aggregations
        self._joins = joins
        self._refs = refs

    def __nonzero__(self):
        raise NotImplementedError('Truth value of an aggregable collection is undefined')

    def __eq__(self, other):
        raise NotImplementedError('Comparison of aggregable collections is undefined')

    def __ne__(self, other):
        raise NotImplementedError('Comparison of aggregable collections is undefined')


class StructExpression(Mapping, Expression):
    """Expression of type :class:`.TStruct`.

    >>> s = hl.capture(hl.Struct(a=5, b='Foo'))

    Struct fields are accessible as attributes and keys. It is therefore
    possible to access field `a` of struct `s` with dot syntax:

    .. doctest::

        >>> hl.eval_expr(s.a)
        5

    However, it is recommended to use square brackets to select fields:

    .. doctest::

        >>> hl.eval_expr(s['a'])
        5

    The latter syntax is safer, because fields that share their name with
    an existing attribute of :class:`.StructExpression` (`keys`, `values`,
    `annotate`, `drop`, etc.) will only be accessible using the
    :meth:`.StructExpression.__getitem__` syntax. This is also the only way
    to access fields that are not valid Python identifiers, like fields with
    spaces or symbols.
    """

    def _init(self):
        self._fields = OrderedDict()

        for fd in self._type.fields:
            expr = construct_expr(Select(self._ast, fd.name), fd.typ, self._indices,
                                  self._aggregations, self._joins, self._refs)
            self._set_field(fd.name, expr)

    def _set_field(self, key, value):
        self._fields[key] = value
        if key not in self.__dict__:
            self.__dict__[key] = value

    def __len__(self):
        return len(self._fields)

    @typecheck_method(item=strlike)
    def __getitem__(self, item):
        """Access a field of the struct.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s['a'])
            5

        Parameters
        ----------
        item : :obj:`str`
            Field name.

        Returns
        -------
        :class:`.Expression`
            Struct field.
        """
        if not item in self._fields:
            raise KeyError("Struct has no field '{}'\n"
                           "    Fields: [ {} ]".format(item, ', '.join("'{}'".format(x) for x in self._fields)))
        return self._fields[item]

    def __iter__(self):
        return iter(self._fields)

    def __hash__(self):
        return object.__hash__(self)

    def __eq__(self, other):
        return Expression.__eq__(self, other)

    def __ne__(self, other):
        return Expression.__ne__(self, other)

    def __nonzero__(self):
        return Expression.__nonzero__(self)

    @typecheck_method(named_exprs=expr_any)
    def annotate(self, **named_exprs):
        """Add new fields or recompute existing fields.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s.annotate(a=10, c=2*2*2))
            Struct(a=10, b='Foo', c=8)

        Notes
        -----
        If an expression in `named_exprs` shares a name with a field of the
        struct, then that field will be replaced but keep its position in
        the struct. New fields will be appended to the end of the struct.

        Parameters
        ----------
        named_exprs : keyword args of :class:`.Expression`
            Fields to add.

        Returns
        -------
        :class:`.StructExpression`
            Struct with new or updated fields.
        """
        names = []
        types = []
        for fd in self.dtype.fields:
            names.append(fd.name)
            types.append(fd.typ)
        kwargs_struct = to_expr(Struct(**named_exprs))

        for fd in kwargs_struct.dtype.fields:
            if not fd.name in self._fields:
                names.append(fd.name)
                types.append(fd.typ)

        result_type = TStruct(names, types)
        indices, aggregations, joins, refs = unify_all(self, kwargs_struct)

        return construct_expr(ApplyMethod('annotate', self._ast, kwargs_struct._ast), result_type,
                              indices, aggregations, joins, refs)

    @typecheck_method(fields=strlike, named_exprs=expr_any)
    def select(self, *fields, **named_exprs):
        """Select existing fields and compute new ones.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s.select('a', c=['bar', 'baz']))
            Struct(a=5, c=[u'bar', u'baz'])

        Notes
        -----
        The `fields` argument is a list of field names to keep. These fields
        will appear in the resulting struct in the order they appear in
        `fields`.

        The `named_exprs` arguments are new field expressions.

        Parameters
        ----------
        fields : varargs of :obj:`str`
            Field names to keep.
        named_exprs : keyword args of :class:`.Expression`
            New field expressions.

        Returns
        -------
        :class:`.StructExpression`
            Struct containing specified existing fields and computed fields.
        """
        names = []
        name_set = set()
        types = []
        for a in fields:
            if not a in self._fields:
                raise KeyError("Struct has no field '{}'\n"
                               "    Fields: [ {} ]".format(a, ', '.join("'{}'".format(x) for x in self._fields)))
            if a in name_set:
                raise ExpressionException("'StructExpression.select' does not support duplicate identifiers.\n"
                                          "    Identifier '{}' appeared more than once".format(a))
            names.append(a)
            name_set.add(a)
            types.append(self[a].dtype)
        select_names = names[:]
        select_name_set = set(select_names)

        kwargs_struct = to_expr(Struct(**named_exprs))
        for fd in kwargs_struct.dtype.fields:
            if fd.name in select_name_set:
                raise ExpressionException("Cannot select and assign '{}' in the same 'select' call".format(fd.name))
            names.append(fd.name)
            types.append(fd.typ)
        result_type = TStruct(names, types)

        indices, joins, aggregations, refs = unify_all(self, kwargs_struct)

        return construct_expr(ApplyMethod('merge', StructOp('select', self._ast, *select_names), kwargs_struct._ast),
                              result_type, indices, joins, aggregations, refs)

    @typecheck_method(fields=strlike)
    def drop(self, *fields):
        """Drop fields from the struct.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s.drop('b'))
            Struct(a=5)

        Parameters
        ----------
        fields: varargs of :obj:`str`
            Fields to drop.

        Returns
        -------
        :class:`.StructExpression`
            Struct without certain fields.
        """
        to_drop = set()
        for a in fields:
            if not a in self._fields:
                raise KeyError("Struct has no field '{}'\n"
                               "    Fields: [ {} ]".format(a, ', '.join("'{}'".format(x) for x in self._fields)))
            if a in to_drop:
                warn("Found duplicate field name in 'StructExpression.drop': '{}'".format(a))
            to_drop.add(a)

        names = []
        types = []
        for fd in self.dtype.fields:
            if not fd.name in to_drop:
                names.append(fd.name)
                types.append(fd.typ)
        result_type = TStruct(names, types)
        return construct_expr(StructOp('drop', self._ast, *to_drop), result_type,
                              self._indices, self._joins, self._aggregations, self._refs)


class AtomicExpression(Expression):
    """Abstract base class for numeric and logical types."""

    def to_float64(self):
        """Convert to a 64-bit floating point expression.

        Returns
        -------
        :class:`.Float64Expression`
        """
        return self._method("toFloat64", TFloat64())

    def to_float32(self):
        """Convert to a 32-bit floating point expression.

        Returns
        -------
        :class:`.Float32Expression`
        """
        return self._method("toFloat32", TFloat32())

    def to_int64(self):
        """Convert to a 64-bit integer expression.

        Returns
        -------
        :class:`.Int64Expression`
        """
        return self._method("toInt64", TInt64())

    def to_int32(self):
        """Convert to a 32-bit integer expression.

        Returns
        -------
        :class:`.Int32Expression`
        """
        return self._method("toInt32", TInt32())


class BooleanExpression(AtomicExpression):
    """Expression of type :class:`.TBoolean`.

    >>> t = hl.capture(True)
    >>> f = hl.capture(False)
    >>> na = hl.null(hl.TBoolean())

    .. doctest::

        >>> hl.eval_expr(t)
        True

        >>> hl.eval_expr(f)
        False

        >>> hl.eval_expr(na)
        None

    """

    def _bin_op_logical(self, name, other):
        other = to_expr(other)
        return self._bin_op(name, other, TBoolean())

    @typecheck_method(other=expr_bool)
    def __and__(self, other):
        """Return ``True`` if the left and right arguments are ``True``.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(t & f)
            False

            >>> hl.eval_expr(t & na)
            None

            >>> hl.eval_expr(f & na)
            False

        The ``&`` and ``|`` operators have higher priority than comparison
        operators like ``==``, ``<``, or ``>``. Parentheses are often
        necessary:

        .. doctest::

            >>> x = hl.capture(5)

            >>> hl.eval_expr((x < 10) & (x > 2))
            True

        Parameters
        ----------
        other : :class:`.BooleanExpression`
            Right-side operand.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if both left and right are ``True``.
        """
        return self._bin_op_logical("&&", other)

    @typecheck_method(other=expr_bool)
    def __or__(self, other):
        """Return ``True`` if at least one of the left and right arguments is ``True``.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(t | f)
            True

            >>> hl.eval_expr(t | na)
            True

            >>> hl.eval_expr(f | na)
            None

        The ``&`` and ``|`` operators have higher priority than comparison
        operators like ``==``, ``<``, or ``>``. Parentheses are often
        necessary:

        .. doctest::

            >>> x = hl.capture(5)

            >>> hl.eval_expr((x < 10) | (x > 20))
            True

        Parameters
        ----------
        other : :class:`.BooleanExpression`
            Right-side operand.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if either left or right is ``True``.
        """
        return self._bin_op_logical("||", other)

    def __invert__(self):
        """Return the boolean negation.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(~t)
            False

            >>> hl.eval_expr(~f)
            True

            >>> hl.eval_expr(~na)
            None

        Returns
        -------
        :class:`.BooleanExpression`
            Boolean negation.
        """
        return self._unary_op("!")


class NumericExpression(AtomicExpression):
    """Expression of numeric type.

    >>> x = hl.capture(3)

    >>> y = hl.capture(4.5)
    """

    def _bin_op_ret_typ(self, other):
        if isinstance(other._type, TArray):
            t = other._type.element_type
            wrapper = lambda t: TArray(t)
        else:
            t = other._type
            wrapper = lambda t: t
        t = unify_types(self._type, t)
        if not t:
            return None
        else:
            return t, wrapper

    def _bin_op_numeric(self, name, other, ret_type_f=None):
        other = to_expr(other)
        ret_type, wrapper = self._bin_op_ret_typ(other)
        if not ret_type:
            raise NotImplementedError("'{}' {} '{}'".format(
                self._type, name, other._type))
        if ret_type_f:
            ret_type = ret_type_f(ret_type)
        return self._bin_op(name, other, wrapper(ret_type))

    def _bin_op_numeric_reverse(self, name, other, ret_type_f=None):
        other = to_expr(other)
        ret_type, wrapper = self._bin_op_ret_typ(other)
        if not ret_type:
            raise NotImplementedError("'{}' {} '{}'".format(
                self._type, name, other._type))
        if ret_type_f:
            ret_type = ret_type_f(ret_type)
        return self._bin_op_reverse(name, other, wrapper(ret_type))

    @typecheck_method(other=expr_numeric)
    def __lt__(self, other):
        """Less-than comparison.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(x < 5)
            True

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Right side for comparison.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the left side is smaller than the right side.
        """
        return self._bin_op("<", other, TBoolean())

    @typecheck_method(other=expr_numeric)
    def __le__(self, other):
        """Less-than-or-equals comparison.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(x <= 3)
            True

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Right side for comparison.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the left side is smaller than or equal to the right side.
        """
        return self._bin_op("<=", other, TBoolean())

    @typecheck_method(other=expr_numeric)
    def __gt__(self, other):
        """Greater-than comparison.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(y > 4)
            True

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Right side for comparison.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the left side is greater than the right side.
        """
        return self._bin_op(">", other, TBoolean())

    @typecheck_method(other=expr_numeric)
    def __ge__(self, other):
        """Greater-than-or-equals comparison.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(y >= 4)
            True

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Right side for comparison.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the left side is greater than or equal to the right side.
        """
        return self._bin_op(">=", other, TBoolean())

    def __pos__(self):
        return self

    def __neg__(self):
        """Negate the number (multiply by -1).

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(-x)
            -3

        Returns
        -------
        :class:`.NumericExpression`
            Negated number.
        """
        return self._unary_op("-")

    def __add__(self, other):
        """Add two numbers.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(x + 2)
            5

            >>> hl.eval_expr(x + y)
            7.5

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Number to add.

        Returns
        -------
        :class:`.NumericExpression`
            Sum of the two numbers.
        """
        return self._bin_op_numeric("+", other)

    def __radd__(self, other):
        return self._bin_op_numeric_reverse("+", other)

    def __sub__(self, other):
        """Subtract the right number from the left.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(x - 2)
            1

            >>> hl.eval_expr(x - y)
            -1.5

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Number to subtract.

        Returns
        -------
        :class:`.NumericExpression`
            Difference of the two numbers.
        """
        return self._bin_op_numeric("-", other)

    def __rsub__(self, other):
        return self._bin_op_numeric_reverse("-", other)

    def __mul__(self, other):
        """Multiply two numbers.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(x * 2)
            6

            >>> hl.eval_expr(x * y)
            9.0

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Number to multiply.

        Returns
        -------
        :class:`.NumericExpression`
            Product of the two numbers.
        """
        return self._bin_op_numeric("*", other)

    def __rmul__(self, other):
        return self._bin_op_numeric_reverse("*", other)

    def __div__(self, other):
        """Divide two numbers.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(x / 2)
            1.5

            >>> hl.eval_expr(y / 0.1)
            45.0

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Dividend.

        Returns
        -------
        :class:`.NumericExpression`
            The left number divided by the left.
        """

        def ret_type_f(t):
            assert is_numeric(t)
            if isinstance(t, TInt32) or isinstance(t, TInt64):
                return TFloat32()
            else:
                # Float64 or Float32
                return t

        return self._bin_op_numeric("/", other, ret_type_f)

    def __rdiv__(self, other):
        def ret_type_f(t):
            assert is_numeric(t)
            if isinstance(t, TInt32) or isinstance(t, TInt64):
                return TFloat32()
            else:
                # Float64 or Float32
                return t

        return self._bin_op_numeric_reverse("/", other, ret_type_f)

    def __floordiv__(self, other):
        """Divide two numbers with floor division.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(x // 2)
            1

            >>> hl.eval_expr(y // 2)
            2.0

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Dividend.

        Returns
        -------
        :class:`.NumericExpression`
            The floor of the left number divided by the right.
        """
        return self._bin_op_numeric('//', other)

    def __rfloordiv__(self, other):
        return self._bin_op_numeric_reverse('//', other)

    def __mod__(self, other):
        """Compute the left modulo the right number.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(32 % x)
            2

            >>> hl.eval_expr(7 % y)
            2.5

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Dividend.

        Returns
        -------
        :class:`.NumericExpression`
            Remainder after dividing the left by the right.
        """
        return self._bin_op_numeric('%', other)

    def __rmod__(self, other):
        return self._bin_op_numeric_reverse('%', other)

    def __pow__(self, power, modulo=None):
        """Raise the left to the right power.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(x ** 2)
            9.0

            >>> hl.eval_expr(x ** -2)
            0.1111111111111111

            >>> hl.eval_expr(y ** 1.5)
            9.545941546018392

        Parameters
        ----------
        power : :class:`.NumericExpression`
        modulo
            Unsupported argument.

        Returns
        -------
        :class:`.Float64Expression`
            Result of raising left to the right power.
        """
        return self._bin_op_numeric('**', power, lambda _: TFloat64())

    def __rpow__(self, other):
        return self._bin_op_numeric_reverse('**', other, lambda _: TFloat64())

    def signum(self):
        """Returns the sign of the callee, ``1`` or ``-1``.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(y.signum())
            1

        Returns
        -------
        :class:`.Int32Expression`
            ``1`` or ``-1``.
        """
        return self._method("signum", TInt32())

    def abs(self):
        """Returns the absolute value of the callee.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(y.abs())
            4.5

        Returns
        -------
        :class:`.NumericExpression`
            Absolute value of the number.
        """
        return self._method("abs", self._type)

    @typecheck_method(other=expr_numeric)
    def max(self, other):
        """Returns the maximum value between the callee and `other`.

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Value to compare against.

        Returns
        -------
        :class:`.NumericExpression`
            Maximum value.
        """
        t = unify_types(self._type, other._type)
        assert (t is not None)
        return self._method("max", t, other)

    @typecheck_method(other=expr_numeric)
    def min(self, other):
        """Returns the minumum value between the callee and `other`.

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Value to compare against.

        Returns
        -------
        :class:`.NumericExpression`
            Minimum value.
        """
        t = unify_types(self._type, other._type)
        assert (t is not None)
        return self._method("min", t, other)


class Float64Expression(NumericExpression):
    """Expression of type :class:`.TFloat64`."""
    pass


class Float32Expression(NumericExpression):
    """Expression of type :class:`.TFloat32`."""
    pass


class Int32Expression(NumericExpression):
    """Expression of type :class:`.TInt32`."""
    pass


class Int64Expression(NumericExpression):
    """Expression of type :class:`.TInt64`."""
    pass


class StringExpression(AtomicExpression):
    """Expression of type :class:`.TString`.

    >>> s = hl.capture('The quick brown fox')
    """

    def __getitem__(self, item):
        """Slice or index into the string.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s[:15])
            'The quick brown'

            >>> hl.eval_expr(s[0])
            'T'

        Parameters
        ----------
        item : slice or :class:`.Int32Expression`
            Slice or character index.

        Returns
        -------
        :class:`.StringExpression`
            Substring or character at index `item`.
        """
        if isinstance(item, slice):
            return self._slice(TString(), item.start, item.stop, item.step)
        else:
            item = to_expr(item)
            if not isinstance(item._type, TInt32):
                raise TypeError("String expects index to be type 'slice' or expression of type 'Int32', "
                                "found expression of type '{}'".format(item._type))
            return self._index(TString(), item)

    def __add__(self, other):
        """Concatenate strings.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s + ' jumped over the lazy dog')
            'The quick brown fox jumped over the lazy dog'

        Parameters
        ----------
        other : :class:`.StringExpression`
            String to concatenate.

        Returns
        -------
        :class:`.StringExpression`
            Concatenated string.
        """
        other = to_expr(other)
        if not isinstance(other._type, TString):
            raise NotImplementedError("'{}' + '{}'".format(self._type, other._type))
        return self._bin_op("+", other, self._type)

    def __radd__(self, other):
        other = to_expr(other)
        if not isinstance(other._type, TString):
            raise NotImplementedError("'{}' + '{}'".format(other._type, self._type))
        return self._bin_op_reverse("+", other, self._type)

    def length(self):
        """Returns the length of the string.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s.length())
            19

        Returns
        -------
        :class:`.Int32Expression`
            Length of the string.
        """
        return self._method("length", TInt32())

    @typecheck_method(pattern1=expr_str, pattern2=expr_str)
    def replace(self, pattern1, pattern2):
        """Replace substrings matching `pattern1` with `pattern2` using regex.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s.replace(' ', '_'))
            'The_quick_brown_fox'

        Notes
        -----
        The regex expressions used should follow
        `Java regex syntax <https://docs.oracle.com/javase/8/docs/api/java/util/regex/Pattern.html>`_

        Parameters
        ----------
        pattern1 : str or :class:`.StringExpression`
        pattern2 : str or :class:`.StringExpression`

        Returns
        -------

        """
        return self._method("replace", TString(), pattern1, pattern2)

    @typecheck_method(delim=expr_str, n=nullable(expr_int32))
    def split(self, delim, n=None):
        """Returns an array of strings generated by splitting the string at `delim`.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(s.split('\\s+'))
            ['The', 'quick', 'brown', 'fox']

            >>> hl.eval_expr(s.split('\\s+', 2))
            ['The', 'quick brown fox']

        Notes
        -----
        The delimiter is a regex using the
        `Java regex syntax <https://docs.oracle.com/javase/8/docs/api/java/util/regex/Pattern.html>`_
        delimiter. To split on special characters, escape them with double
        backslash (``\\\\``).

        Parameters
        ----------
        delim : str or :class:`.StringExpression`
            Delimiter regex.
        n : :class:`.Int32Expression`, optional
            Maximum number of splits.

        Returns
        -------
        :class:`.ArrayExpression`
            Array of split strings.
        """
        if n is None:
            return self._method("split", TArray(TString()), delim)
        else:
            return self._method("split", TArray(TString()), delim, n)

    @typecheck_method(regex=strlike)
    def matches(self, regex):
        """Returns ``True`` if the string contains any match for the given regex.

        Examples
        --------

        >>> string = hl.capture('NA12878')

        The `regex` parameter does not need to match the entire string:

        .. doctest::

            >>> hl.eval_expr(string.matches('12'))
            True

        Regex motifs can be used to match sequences of characters:

        .. doctest::

            >>> hl.eval_expr(string.matches(r'NA\\\\d+'))
            True

        Notes
        -----
        The `regex` argument is a
        `regular expression <https://en.wikipedia.org/wiki/Regular_expression>`__,
        and uses
        `Java regex syntax <https://docs.oracle.com/javase/8/docs/api/java/util/regex/Pattern.html>`__.

        Parameters
        ----------
        regex: :obj:`str`
            Pattern to match.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the string contains any match for the regex, otherwise ``False``.
        """
        return construct_expr(RegexMatch(self._ast, regex), TBoolean(),
                              self._indices, self._aggregations, self._joins, self._refs)

    def to_int32(self):
        """Parse the string to a 32-bit integer.

        Examples
        --------
        .. doctest::

            >>> s = hl.capture('123')
            >>> hl.eval_expr(s.to_int32())
            123

        Returns
        -------
        :class:`.Int32Expression`
            Parsed integer expression.
        """
        return self._method("toInt32", TInt32())

    def to_int64(self):
        """Parse the string to a 64-bit integer.

        Examples
        --------
        .. doctest::

            >>> s = hl.capture('123123123123')
            >>> hl.eval_expr(s.to_int64())
            123123123123L

        Returns
        -------
        :class:`.Int64Expression`
            Parsed integer expression.
        """

        return self._method("toInt64", TInt64())

    def to_float32(self):
        """Parse the string to a 32-bit float.

        Examples
        --------
        .. doctest::

            >>> s = hl.capture('1.5')
            >>> hl.eval_expr(s.to_float32())
            1.5

        Returns
        -------
        :class:`.Float32Expression`
            Parsed float expression.
        """
        return self._method("toFloat32", TFloat32())

    def to_float64(self):
        """Parse the string to a 64-bit float.

        Examples
        --------
        .. doctest::

            >>> s = hl.capture('1.15e80')
            >>> hl.eval_expr(s.to_float64())
            1.15e+80

        Returns
        -------
        :class:`.Float64Expression`
            Parsed float expression.
        """
        return self._method("toFloat64", TFloat64())

    def to_boolean(self):
        """Parse the string to a Boolean.

        Examples
        --------
        .. doctest::

            >>> s = hl.capture('TRUE')
            >>> hl.eval_expr(s.to_boolean())
            True

        Notes
        -----
        Acceptable values are: ``True``, ``true``, ``TRUE``, ``False``,
        ``false``, and ``FALSE``.

        Returns
        -------
        :class:`.BooleanExpression`
            Parsed Boolean expression.
        """

        return self._method("toBoolean", TBoolean())


class CallExpression(Expression):
    """Expression of type :class:`.TCall`.

    >>> call = hl.call(False, 0, 1)
    """

    def __getitem__(self, item):
        """Get the i*th* allele.

        Examples
        --------

        Index with a single integer:

        .. doctest::

            >>> hl.eval_expr(call[0])
            0

            >>> hl.eval_expr(call[1])
            1

        Parameters
        ----------
        item : int or :class:`.Int32Expression`
            Allele index.

        Returns
        -------
        :class:`.Int32Expression`
        """
        if isinstance(item, slice):
            raise NotImplementedError("CallExpression does not support indexing with a slice.")
        else:
            item = to_expr(item)
            if not isinstance(item._type, TInt32):
                raise TypeError("Call expects allele index to be an expression of type 'Int32', "
                                "found expression of type '{}'".format(item._type))
            return self._index(TInt32(), item)

    @property
    def ploidy(self):
        """Return the number of alleles of this call.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(call.ploidy)
            2

        Returns
        -------
        :class:`.Int32Expression`
        """
        return self._method("ploidy", TInt32())

    @property
    def phased(self):
        """True if the call is phased.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(call.phased)
            False

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("isPhased", TBoolean())

    def is_haploid(self):
        """True if the call has ploidy equal to 1.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(call.is_haploid())
            False

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self.ploidy == 1

    def is_diploid(self):
        """True if the call has ploidy equal to 2.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(call.is_diploid())
            True

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self.ploidy == 2

    def is_non_ref(self):
        """Evaluate whether the call includes one or more non-reference alleles.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(call.is_non_ref())
            True

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if at least one allele is non-reference, ``False`` otherwise.
        """
        return self._method("isNonRef", TBoolean())

    def is_het(self):
        """Evaluate whether the call includes two different alleles.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(call.is_het())
            True

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the two alleles are different, ``False`` if they are the same.
        """
        return self._method("isHet", TBoolean())

    def is_het_nonref(self):
        """Evaluate whether the call includes two different alleles, neither of which is reference.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(call.is_het_nonref())
            False

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the call includes two different alternate alleles, ``False`` otherwise.
        """
        return self._method("isHetNonRef", TBoolean())

    def is_het_ref(self):
        """Evaluate whether the call includes two different alleles, one of which is reference.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(call.is_het_ref())
            True

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the call includes one reference and one alternate allele, ``False`` otherwise.
        """
        return self._method("isHetRef", TBoolean())

    def is_hom_ref(self):
        """Evaluate whether the call includes two reference alleles.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(call.is_hom_ref())
            False

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the call includes two reference alleles, ``False`` otherwise.
        """
        return self._method("isHomRef", TBoolean())

    def is_hom_var(self):
        """Evaluate whether the call includes two identical alternate alleles.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(call.is_hom_var())
            False

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the call includes two identical alternate alleles, ``False`` otherwise.
        """
        return self._method("isHomVar", TBoolean())

    def num_alt_alleles(self):
        """Returns the number of non-reference alleles.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(call.num_alt_alleles())
            1

        Returns
        -------
        :class:`.Int32Expression`
            The number of non-reference alleles.
        """
        return self._method("nNonRefAlleles", TInt32())

    @typecheck_method(alleles=expr_list)
    def one_hot_alleles(self, alleles):
        """Returns an array containing the summed one-hot encoding of the
        alleles.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(call.one_hot_alleles(['A', 'T']))
            [1, 1]

        This one-hot representation is the positional sum of the one-hot
        encoding for each called allele. For a biallelic variant, the one-hot
        encoding for a reference allele is ``[1, 0]`` and the one-hot encoding
        for an alternate allele is ``[0, 1]``. Diploid calls would produce the
        following arrays: ``[2, 0]`` for homozygous reference, ``[1, 1]`` for
        heterozygous, and ``[0, 2]`` for homozygous alternate.

        Parameters
        ----------
        alleles: :class:`.ArrayStringExpression`
            Variant alleles.

        Returns
        -------
        :class:`.ArrayInt32Expression`
            An array of summed one-hot encodings of allele indices.
        """
        return self._method("oneHotAlleles", TArray(TInt32()), alleles)

    def unphased_diploid_gt_index(self):
        """Return the genotype index for unphased, diploid calls.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(call.unphased_diploid_gt_index())
            1

        Returns
        -------
        :class:`.Int32Expression`
        """
        return self._method("unphasedDiploidGtIndex", TInt32())

class LocusExpression(Expression):
    """Expression of type :class:`.TLocus`.

    >>> locus = hl.capture(hl.Locus('1', 100))
    """

    @property
    def contig(self):
        """Returns the chromosome.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(locus.contig)
            '1'

        Returns
        -------
        :class:`.StringExpression`
            The chromosome for this locus.
        """
        return self._field("contig", TString())

    @property
    def position(self):
        """Returns the position along the chromosome.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(locus.position)
            100

        Returns
        -------
        :class:`.Int32Expression`
            This locus's position along its chromosome.
        """
        return self._field("position", TInt32())

    def in_x_nonpar(self):
        """Returns ``True`` if the locus is in a non-pseudoautosomal
        region of chromosome X.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(locus.in_x_nonpar())
            False

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("inXNonPar", TBoolean())

    def in_x_par(self):
        """Returns ``True`` if the locus is in a pseudoautosomal region
        of chromosome X.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(locus.in_x_par())
            False

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("inXPar", TBoolean())

    def in_y_nonpar(self):
        """Returns ``True`` if the locus is in a non-pseudoautosomal
        region of chromosome Y.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(locus.in_y_nonpar())
            False

        Note
        ----
        Many variant callers only generate variants on chromosome X for the
        pseudoautosomal region. In this case, all loci mapped to chromosome
        Y are non-pseudoautosomal.

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("inYNonPar", TBoolean())

    def in_y_par(self):
        """Returns ``True`` if the locus is in a pseudoautosomal region
        of chromosome Y.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(locus.in_y_par())
            False

        Note
        ----
        Many variant callers only generate variants on chromosome X for the
        pseudoautosomal region. In this case, all loci mapped to chromosome
        Y are non-pseudoautosomal.

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("inYPar", TBoolean())

    def in_autosome(self):
        """Returns ``True`` if the locus is on an autosome.

        Notes
        -----
        All contigs are considered autosomal except those
        designated as X, Y, or MT by :class:`.GenomeReference`.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(locus.in_autosome())
            True

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("isAutosomal", TBoolean())

    def in_autosome_or_par(self):
        """Returns ``True`` if the locus is on an autosome or
        a pseudoautosomal region of chromosome X or Y.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(locus.in_autosome_or_par())
            True

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("isAutosomalOrPseudoAutosomal", TBoolean())

    def in_mito(self):
        """Returns ``True`` if the locus is on mitochondrial DNA.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(locus.in_mito())
            True

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("isMitochondrial", TBoolean())


class IntervalExpression(Expression):
    """Expression of type :class:`.TInterval`.

    >>> interval = hl.capture(hl.Interval.parse('X:1M-2M'))
    """

    @typecheck_method(locus=expr_locus)
    def contains(self, locus):
        """Tests whether a locus is contained in the interval.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(interval.contains(hl.Locus('X', 3000000)))
            False

            >>> hl.eval_expr(interval.contains(hl.Locus('X', 1500000)))
            True

        Parameters
        ----------
        locus : :class:`.Locus` or :class:`.LocusExpression`
            Locus to test for interval membership.
        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if `locus` is contained in the interval, ``False`` otherwise.
        """
        locus = to_expr(locus)
        if self._type.point_type != locus._type:
            raise TypeError('expected {}, found: {}'.format(self._type.point_type, locus._type))
        return self._method("contains", TBoolean(), locus)

    @property
    def end(self):
        """Returns the end locus.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(interval.end)
            Locus(contig=X, position=2000000, reference_genome=GRCh37)

        Returns
        -------
        :class:`.LocusExpression`
            End locus.
        """
        return self._field("end", TLocus())

    @property
    def start(self):
        """Returns the start locus.

        Examples
        --------
        .. doctest::

            >>> hl.eval_expr(interval.start)
            Locus(contig=X, position=1000000, reference_genome=GRCh37)

        Returns
        -------
        :class:`.LocusExpression`
            Start locus.
        """
        return self._field("start", TLocus())


typ_to_expr = {
    TBoolean: BooleanExpression,
    TInt32: Int32Expression,
    TInt64: Int64Expression,
    TFloat64: Float64Expression,
    TFloat32: Float32Expression,
    TLocus: LocusExpression,
    TInterval: IntervalExpression,
    TCall: CallExpression,
    TString: StringExpression,
    TDict: DictExpression,
    TArray: ArrayExpression,
    TSet: SetExpression,
    TStruct: StructExpression
}

elt_typ_to_array_expr = {
    TBoolean: ArrayBooleanExpression,
    TInt32: ArrayInt32Expression,
    TFloat64: ArrayFloat64Expression,
    TInt64: ArrayInt64Expression,
    TFloat32: ArrayFloat32Expression,
    TString: ArrayStringExpression,
    TStruct: ArrayStructExpression,
    TArray: ArrayArrayExpression
}

elt_typ_to_set_expr = {
    TInt32: SetInt32Expression,
    TFloat64: SetFloat64Expression,
    TFloat32: SetFloat32Expression,
    TInt64: SetFloat32Expression,
    TString: SetStringExpression,
    TSet: SetSetExpression
}


class ExpressionException(Exception):
    def __init__(self, msg=''):
        self.msg = msg
        super(ExpressionException, self).__init__(msg)


class ExpressionWarning(Warning):
    def __init__(self, msg=''):
        self.msg = msg
        super(ExpressionWarning, self).__init__(msg)


@typecheck(caller=strlike, expr=Expression,
           expected_indices=Indices,
           aggregation_axes=setof(strlike))
def analyze(caller, expr, expected_indices, aggregation_axes=set()):
    indices = expr._indices
    source = indices.source
    axes = indices.axes
    aggregations = expr._aggregations
    refs = expr._refs

    warnings = []
    errors = []

    expected_source = expected_indices.source
    expected_axes = expected_indices.axes

    if source is not None and source is not expected_source:
        errors.append(
            ExpressionException('{} expects an expression from source {}, found expression derived from {}'.format(
                caller, expected_source, source
            )))

    # check for stray indices by subtracting expected axes from observed
    unexpected_axes = axes - expected_axes

    if unexpected_axes:
        # one or more out-of-scope fields
        assert not refs.empty()
        bad_refs = []
        for name, inds in refs:
            bad_axes = inds.axes.intersection(unexpected_axes)
            if bad_axes:
                bad_refs.append((name, inds))

        assert len(bad_refs) > 0
        errors.append(ExpressionException(
            "scope violation: '{caller}' expects an expression indexed by {expected}"
            "\n    Found indices {axes}, with unexpected indices {stray}. Invalid fields:{fields}{agg}".format(
                caller=caller,
                expected=list(expected_axes),
                axes=list(indices.axes),
                stray=list(unexpected_axes),
                fields=''.join("\n        '{}' (indices {})".format(name, list(inds.axes)) for name, inds in bad_refs),
                agg='' if (unexpected_axes - aggregation_axes) else
                "\n    '{}' supports aggregation over axes {}, "
                "so these fields may appear inside an aggregator function.".format(caller, list(aggregation_axes))
            )))

    if aggregations:
        if aggregation_axes:

            # the expected axes of aggregated expressions are the expected axes + axes aggregated over
            expected_agg_axes = expected_axes.union(aggregation_axes)

            for agg in aggregations:
                agg_indices = agg.indices
                agg_axes = agg_indices.axes
                if agg_indices.source is not None and agg_indices.source is not expected_source:
                    errors.append(
                        ExpressionException(
                            'Expected an expression from source {}, found expression derived from {}'
                            '\n    Invalid fields: [{}]'.format(
                                expected_source, source, ', '.join("'{}'".format(name) for name, _ in agg.refs)
                            )))

                # check for stray indices
                unexpected_agg_axes = agg_axes - expected_agg_axes
                if unexpected_agg_axes:
                    # one or more out-of-scope fields
                    bad_refs = []
                    for name, inds in agg.refs:
                        bad_axes = inds.axes.intersection(unexpected_agg_axes)
                        if bad_axes:
                            bad_refs.append((name, inds))

                    errors.append(ExpressionException(
                        "scope violation: '{caller}' supports aggregation over indices {expected}"
                        "\n    Found indices {axes}, with unexpected indices {stray}. Invalid fields:{fields}".format(
                            caller=caller,
                            expected=list(aggregation_axes),
                            axes=list(agg_axes),
                            stray=list(unexpected_agg_axes),
                            fields=''.join("\n        '{}' (indices {})".format(
                                name, list(inds.axes)) for name, inds in bad_refs)
                        )
                    ))
        else:
            errors.append(ExpressionException("'{}' does not support aggregation".format(caller)))

    for w in warnings:
        warn('{}'.format(w.msg))
    if errors:
        for e in errors:
            error('{}'.format(e.msg))
        raise errors[0]


@handle_py4j
@typecheck(expression=expr_any)
def eval_expr(expression):
    """Evaluate a Hail expression, returning the result.

    This method is extremely useful for learning about Hail expressions and understanding
    how to compose them.

    Expressions that refer to fields of :class:`.hail.Table` or :class:`.hail.MatrixTable`
    objects cannot be evaluated.

    Examples
    --------
    Evaluate a conditional:

    .. doctest::

        >>> x = 6
        >>> hl.eval_expr(hl.cond(x % 2 == 0, 'Even', 'Odd'))
        'Even'

    Parameters
    ----------
    expression : :class:`.Expression`
        Any expression, or a Python value that can be implicitly interpreted as an expression.

    Returns
    -------
    any
        Result of evaluating `expression`.
    """
    return eval_expr_typed(expression)[0]


@handle_py4j
@typecheck(expression=expr_any)
def eval_expr_typed(expression):
    """Evaluate a Hail expression, returning the result and the type of the result.

    This method is extremely useful for learning about Hail expressions and understanding
    how to compose them.

    Expressions that refer to fields of :class:`.hail.Table` or :class:`.hail.MatrixTable`
    objects cannot be evaluated.

    Examples
    --------
    Evaluate a conditional:

    .. doctest::

        >>> x = 6
        >>> hl.eval_expr_typed(hl.cond(x % 2 == 0, 'Even', 'Odd'))
        ('Odd', TString())

    Parameters
    ----------
    expression : :class:`.Expression`
        Any expression, or a Python value that can be implicitly interpreted as an expression.

    Returns
    -------
    (any, :class:`.Type`)
        Result of evaluating `expression`, and its type.
    """
    analyze('eval_expr_typed', expression, Indices())
    if not expression._joins.empty():
        raise ExpressionException("'eval_expr' methods do not support joins or broadcasts")

    x = Env.hc()._jhc.eval(expression._ast.to_hql())
    t = Type._from_java(x._2())
    r = t._convert_to_py(x._1())
    return r, t


_lazy_int32.set(Int32Expression)
_lazy_numeric.set(NumericExpression)
_lazy_array.set(ArrayExpression)
_lazy_set.set(SetExpression)
_lazy_bool.set(BooleanExpression)
_lazy_struct.set(StructExpression)
_lazy_string.set(StringExpression)
_lazy_locus.set(LocusExpression)
_lazy_interval.set(IntervalExpression)
_lazy_call.set(CallExpression)
_lazy_expr.set(Expression)
