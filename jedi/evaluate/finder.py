import copy

from jedi._compatibility import hasattr, unicode, u
from jedi.parser import representation as pr
from jedi import debug
from jedi import common
from jedi import settings
from jedi.evaluate import representation as er
from jedi.evaluate import dynamic
from jedi.evaluate import builtin
from jedi.evaluate import docstrings
from jedi.evaluate import iterable


class NameFinder(object):
    def __init__(self, evaluator, scope, name_str, position=None):
        self._evaluator = evaluator
        self.scope = scope
        self.name_str = name_str
        self.position = position

    def _resolve_descriptors(self, types):
        """Processes descriptors"""
        result = []
        for r in types:
            if isinstance(self.scope, (er.Instance, er.Class)) \
                    and hasattr(r, 'get_descriptor_return'):
                # handle descriptors
                with common.ignored(KeyError):
                    result += r.get_descriptor_return(self.scope)
                    continue
            result.append(r)
        return result

    def _remove_statements(self, result, resolve_decorator=True):
        """
        This is the part where statements are being stripped.

        Due to lazy evaluation, statements like a = func; b = a; b() have to be
        evaluated.
        """
        evaluator = self._evaluator
        res_new = []
        for r in result:
            add = []
            if r.isinstance(pr.Statement):
                check_instance = None
                if isinstance(r, er.InstanceElement) and r.is_class_var:
                    check_instance = r.instance
                    r = r.var

                # Global variables handling.
                if r.is_global():
                    for token_name in r.token_list[1:]:
                        if isinstance(token_name, pr.Name):
                            add = evaluator.find_types(r.parent, str(token_name))
                else:
                    # generated objects are used within executions, but these
                    # objects are in functions, and we have to dynamically
                    # execute first.
                    if isinstance(r, pr.Param):
                        func = r.parent
                        # Instances are typically faked, if the instance is not
                        # called from outside. Here we check it for __init__
                        # functions and return.
                        if isinstance(func, er.InstanceElement) \
                                and func.instance.is_generated \
                                and hasattr(func, 'name') \
                                and str(func.name) == '__init__' \
                                and r.position_nr > 0:  # 0 would be self
                            r = func.var.params[r.position_nr]

                        # add docstring knowledge
                        doc_params = docstrings.follow_param(evaluator, r)
                        if doc_params:
                            res_new += doc_params
                            continue

                        if not r.is_generated:
                            res_new += dynamic.search_params(evaluator, r)
                            if not res_new:
                                c = r.expression_list()[0]
                                if c in ('*', '**'):
                                    t = 'tuple' if c == '*' else 'dict'
                                    res_new = evaluator.execute(evaluator.find_types(builtin.Builtin.scope, t)[0])
                            if not r.assignment_details:
                                # this means that there are no default params,
                                # so just ignore it.
                                continue

                    # Remove the statement docstr stuff for now, that has to be
                    # implemented with the evaluator class.
                    #if r.docstr:
                        #res_new.append(r)

                    scopes = evaluator.eval_statement(r, seek_name=self.name_str)
                    add += self._remove_statements(scopes)

                if check_instance is not None:
                    # class renames
                    add = [er.InstanceElement(evaluator, check_instance, a, True)
                           if isinstance(a, (er.Function, pr.Function))
                           else a for a in add]
                res_new += add
            else:
                if isinstance(r, pr.Class):
                    r = er.Class(evaluator, r)
                elif isinstance(r, pr.Function):
                    r = er.Function(evaluator, r)
                if r.isinstance(er.Function) and resolve_decorator:
                    r = r.get_decorated_func()
                res_new.append(r)
        debug.dbg('sfn remove, new: %s, old: %s' % (res_new, result))
        return res_new

    def _handle_for_loops(self, loop):
        # Take the first statement (for has always only
        # one, remember `in`). And follow it.
        if not loop.inputs:
            return []
        result = iterable.get_iterator_types(self._evaluator.eval_statement(loop.inputs[0]))
        if len(loop.set_vars) > 1:
            expression_list = loop.set_stmt.expression_list()
            # loops with loop.set_vars > 0 only have one command
            from jedi import evaluate
            result = evaluate._assign_tuples(expression_list[0], result, self.name_str)
        return result

    def _some_method(self, name):
        """
        Returns the parent of a name, which means the element which stands
        behind a name.
        """
        result = []
        par = name.parent
        exc = pr.Class, pr.Function
        until = lambda: par.parent.parent.get_parent_until(exc)

        if par.isinstance(pr.Flow):
            if par.command == 'for':
                result += self._handle_for_loops(par)
            else:
                raise NotImplementedError("Shouldn't happen!")
        elif par.isinstance(pr.Param) \
                and par.parent is not None \
                and isinstance(until(), pr.Class) \
                and par.position_nr == 0:
            # This is where self gets added - this happens at another
            # place, if the var_args are clear. But sometimes the class is
            # not known. Therefore add a new instance for self. Otherwise
            # take the existing.
            if isinstance(self.scope, er.InstanceElement):
                result.append(self.scope.instance)
            else:
                for inst in self._evaluator.execute(er.Class(self._evaluator, until())):
                    inst.is_generated = True
                    result.append(inst)
        elif par is not None:
            result.append(par)
        return result

    def _process(self, name):
        """
        Returns the parent of a name, which means the element which stands
        behind a name.
        """
        no_break_scope = False
        par = name.parent
        is_array_assignment = False

        if par.isinstance(pr.Statement):
            def is_execution(calls):
                for c in calls:
                    if isinstance(c, (unicode, str)):
                        continue
                    if c.isinstance(pr.Array):
                        if is_execution(c):
                            return True
                    elif c.isinstance(pr.Call):
                        # Compare start_pos, because names may be different
                        # because of executions.
                        if c.name.start_pos == name.start_pos \
                                and c.execution:
                            return True
                return False

            is_exe = False
            for assignee, op in par.assignment_details:
                is_exe |= is_execution(assignee)

            if is_exe:
                # filter array[3] = ...
                # TODO check executions for dict contents
                is_array_assignment = True
            else:
                details = par.assignment_details
                if details and details[0][1] != '=':
                    no_break_scope = True

                # TODO this makes self variables non-breakable. wanted?
                if isinstance(name, er.InstanceElement) \
                        and not name.is_class_var:
                    no_break_scope = True
        elif isinstance(par, pr.Import) and len(par.namespace) > 1:
            # TODO multi-level import non-breakable
            no_break_scope = True
        return no_break_scope, is_array_assignment

    def filter_name(self, scope_generator, is_goto=False):
        """
        Filters all variables of a scope (which are defined in the
        `scope_generator`), until the name fits.
        """
        result = []
        for nscope, name_list in scope_generator:
            break_scopes = []
            # here is the position stuff happening (sorting of variables)
            for name in sorted(name_list, key=lambda n: n.start_pos, reverse=True):
                p = name.parent.parent if name.parent else None
                if isinstance(p, er.InstanceElement) \
                        and isinstance(p.var, pr.Class):
                    p = p.var
                if self.name_str == name.get_code() and p not in break_scopes:
                    no_break_scope, is_array_assignment = self._process(name)
                    if not is_array_assignment:  # shouldn't goto arr[1] =
                        result.append(name)
                    # for comparison we need the raw class
                    s = nscope.base if isinstance(nscope, er.Class) else nscope
                    # this means that a definition was found and is not e.g.
                    # in if/else.
                    if result and not no_break_scope:
                        if not name.parent or p == s:
                            break
                        break_scopes.append(p)
            if result:
                break

        if not result and isinstance(self.scope, er.Instance):
            # __getattr__ / __getattribute__
            for r in self._check_getattr(self.scope):
                new_name = copy.copy(r.name)
                new_name.parent = r
                result.append(new_name)

        debug.dbg('sfn filter "%s" in (%s-%s): %s@%s'
                  % (self.name_str, self.scope, nscope, u(result), self.position))
        return result

    def names_to_types(self, names, is_goto=True):
        result = []
        # This adds additional types
        flow_scope = self.scope
        while flow_scope:
            # TODO check if result is in scope -> no evaluation necessary
            n = check_flow_information(self._evaluator, flow_scope,
                                       self.name_str, self.position)
            if n:
                return n
            flow_scope = flow_scope.parent

        for name in names:
            result += self._some_method(name)
        return result

    def _check_getattr(self, inst):
        """Checks for both __getattr__ and __getattribute__ methods"""
        result = []
        module = builtin.Builtin.scope
        # str is important to lose the NamePart!
        name = pr.String(module, "'%s'" % self.name_str, (0, 0), (0, 0), inst)
        with common.ignored(KeyError):
            result = inst.execute_subscope_by_name('__getattr__', [name])
        if not result:
            # this is a little bit special. `__getattribute__` is executed
            # before anything else. But: I know no use case, where this
            # could be practical and the jedi would return wrong types. If
            # you ever have something, let me know!
            with common.ignored(KeyError):
                result = inst.execute_subscope_by_name('__getattribute__', [name])
        return result

    def find(self, scopes, resolve_decorator=True):
        filtered = self.filter_name(scopes)
        filtered = self.names_to_types(filtered)
        return self._resolve_descriptors(self._remove_statements(filtered,
resolve_decorator))

    def scopes(self, search_global=False):
        if search_global:
            return self._evaluator.get_names_of_scope(self.scope, self.position)
        else:
            if isinstance(self.scope, er.Instance):
                return self.scope.scope_generator()
            else:
                if isinstance(self.scope, (er.Class, pr.Module)):
                    # classes are only available directly via chaining?
                    # strange stuff...
                    names = self.scope.get_defined_names()
                else:
                    names = _get_defined_names_for_position(self.scope, self.position)
                return iter([(self.scope, names)])


def check_flow_information(evaluator, flow, search_name, pos):
    """ Try to find out the type of a variable just with the information that
    is given by the flows: e.g. It is also responsible for assert checks.::

        if isinstance(k, str):
            k.  # <- completion here

    ensures that `k` is a string.
    """
    if not settings.dynamic_flow_information:
        return None

    result = []
    if isinstance(flow, pr.IsScope) and not result:
        for ass in reversed(flow.asserts):
            if pos is None or ass.start_pos > pos:
                continue
            result = _check_isinstance_type(evaluator, ass, search_name)
            if result:
                break

    if isinstance(flow, pr.Flow) and not result:
        if flow.command in ['if', 'while'] and len(flow.inputs) == 1:
            result = _check_isinstance_type(evaluator, flow.inputs[0], search_name)
    return result


def _check_isinstance_type(evaluator, stmt, search_name):
    try:
        expression_list = stmt.expression_list()
        # this might be removed if we analyze and, etc
        assert len(expression_list) == 1
        call = expression_list[0]
        assert isinstance(call, pr.Call) and str(call.name) == 'isinstance'
        assert bool(call.execution)

        # isinstance check
        isinst = call.execution.values
        assert len(isinst) == 2  # has two params
        obj, classes = [statement.expression_list() for statement in isinst]
        assert len(obj) == 1
        assert len(classes) == 1
        assert isinstance(obj[0], pr.Call)
        # names fit?
        assert str(obj[0].name) == search_name
        assert isinstance(classes[0], pr.StatementElement)  # can be type or tuple
    except AssertionError:
        return []

    result = []
    for c in evaluator.eval_call(classes[0]):
        for typ in (c.get_index_types() if isinstance(c, iterable.Array) else [c]):
            result += evaluator.execute(typ)
    return result


def _get_defined_names_for_position(scope, position=None, start_scope=None):
    """
    Return filtered version of ``scope.get_defined_names()``.

    This function basically does what :meth:`scope.get_defined_names
    <parsing_representation.Scope.get_defined_names>` does.

    - If `position` is given, delete all names defined after `position`.
    - For special objects like instances, `position` is ignored and all
      names are returned.

    :type     scope: :class:`parsing_representation.IsScope`
    :param    scope: Scope in which names are searched.
    :param position: The position as a line/column tuple, default is infinity.
    """
    names = scope.get_defined_names()
    # Instances have special rules, always return all the possible completions,
    # because class variables are always valid and the `self.` variables, too.
    if (not position or isinstance(scope, (iterable.Array, er.Instance))
       or start_scope != scope
       and isinstance(start_scope, (pr.Function, er.FunctionExecution))):
        return names
    names_new = []
    for n in names:
        if n.start_pos[0] is not None and n.start_pos < position:
            names_new.append(n)
    return names_new
