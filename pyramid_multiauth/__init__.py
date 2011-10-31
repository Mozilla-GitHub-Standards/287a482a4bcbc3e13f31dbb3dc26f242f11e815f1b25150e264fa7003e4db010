# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 1.1/GPL 2.0/LGPL 2.1
#
# The contents of this file are subject to the Mozilla Public License Version
# 1.1 (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
# http://www.mozilla.org/MPL/
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License
# for the specific language governing rights and limitations under the
# License.
#
# The Original Code is pyramid_multiauth
#
# The Initial Developer of the Original Code is the Mozilla Foundation.
# Portions created by the Initial Developer are Copyright (C) 2011
# the Initial Developer. All Rights Reserved.
#
# Contributor(s):
#   Ryan Kelly (ryan@rfk.id.au)
#
# Alternatively, the contents of this file may be used under the terms of
# either the GNU General Public License Version 2 or later (the "GPL"), or
# the GNU Lesser General Public License Version 2.1 or later (the "LGPL"),
# in which case the provisions of the GPL or the LGPL are applicable instead
# of those above. If you wish to allow use of your version of this file only
# under the terms of either the GPL or the LGPL, and not to allow others to
# use your version of this file under the terms of the MPL, indicate your
# decision by deleting the provisions above and replace them with the notice
# and other provisions required by the GPL or the LGPL. If you do not delete
# the provisions above, a recipient may use your version of this file under
# the terms of any one of the MPL, the GPL or the LGPL.
#
# ***** END LICENSE BLOCK *****
"""
Pyramid authn policy that ties together multiple backends.
"""

from zope.interface import implements

from pyramid.interfaces import IAuthenticationPolicy, PHASE2_CONFIG
from pyramid.security import Everyone, Authenticated
from pyramid.authorization import ACLAuthorizationPolicy


class MultiAuthenticationPolicy(object):
    """Pyramid authentication policy for stacked authentication.

    This is a pyramid authentication policy that stitches together *other*
    authentication policies into a flexible auth stack.
    """

    implements(IAuthenticationPolicy)

    def __init__(self, policies, callback=None):
        self._policies = policies
        self._callback = callback

    def authenticated_userid(self, request):
        """Find the authenticated userid for this request.

        This method delegates to each authn policy in turn, taking the
        userid from the first one that doesn't return None.  If a
        groupfinder callback is configured, it is also used to validate
        the userid before returning.
        """
        userid = None
        for policy in self._policies:
            userid = policy.authenticated_userid(request)
            if userid is not None:
                if self._callback is not None:
                    if self._callback(userid) is not None:
                        break
        return userid

    def unauthenticated_userid(self, request):
        """Find the unauthenticated userid for this request.

        This method delegates to each authn policy in turn, taking the
        userid from the first one that doesn't return None.
        """
        userid = None
        for policy in self._policies:
            userid = policy.unauthenticated_userid(request)
            if userid is not None:
                break
        return userid

    def effective_principals(self, request):
        """Get the list of effective principals for this request.

        This method returns the union of the principals returned by each
        authn policy.  If a groupfinder callback is registered, its output
        is also added to the list.
        """
        principals = set((Everyone,))
        for policy in self._policies:
            principals.update(policy.effective_principals(request))
        if self._callback is not None:
            userid = self.unauthenticated_userid(request)
            if userid is not None:
                groups = self._callback(userid, request)
                principals.add(userid)
                principals.add(Authenticated)
                principals.update(groups)
        return list(principals)

    def remember(self, request, principal, **kw):
        """Remember the authenticated userid.

        This method returns the union of the headers returned by each
        authn policy.
        """
        headers = []
        for policy in self._policies:
            headers.extend(policy.remember(request, principal, **kw))
        return headers

    def forget(self, request):
        """Forget a previusly remembered userid.

        This method returns the union of the headers returned by each
        authn policy.
        """
        headers = []
        for policy in self._policies:
            headers.extend(policy.forget(request))
        return headers


def includeme(config):
    """Include default whoauth settings into a pyramid config.

    This function provides a hook for pyramid to include the default settings
    for auth via pyramid_multiauth.  Activate it like so:

        config.include("pyramid_multiauth")

    This will pull the list of registered authn policies from the application
    settings, and configure and install them in order.  The policies to use
    can be specified in one of two ways:

        * as the name of a module to be included.
        * as the name of a callable along with a set of parameters.

    Here's an example suite of settings:

        multiauth.policies = ipauth1 ipauth2 pyramid_browserid

        multiauth.policy.ipauth1.use = pyramid_ipauth.IPAuthentictionPolicy
        multiauth.policy.ipauth1.ipaddrs = 123.123.0.0/16
        multiauth.policy.ipauth1.userid = local1

        multiauth.policy.ipauth2.use = pyramid_ipauth.IPAuthentictionPolicy
        multiauth.policy.ipauth2.ipaddrs = 124.124.0.0/16
        multiauth.policy.ipauth2.userid = local2

    This will configure a MultiAuthenticationPolicy with three policy objects.
    The first two will be IPAuthenticationPolicy objects created by passing
    in the specified keyword arguments.  The third will be a BrowserID
    authentication policy just like you would get from executing:

        config.include("pyramid_browserid")

    As a side-effect, the configuration will also get the additional views
    that pyramid_browserid sets up by default.
    """
    # Grab the pyramid-wide settings, to look for any auth config.
    settings = config.get_settings()
    # Get the groupfinder from config if present.
    groupfinder = settings.get("multiauth.policy.groupfinder", None)
    groupfinder = config.maybe_dotted(groupfinder)
    # Look for callable policy definitions.
    # Suck them all out at once and store them in a dict for later use.
    policy_definitions = {}
    for name, value in settings.iteritems():
        if not name.startswith("multiauth.policy."):
            continue
        name = name[len("multiauth.policy."):]
        policy_name, setting_name = name.split(".", 1)
        if policy_name not in policy_definitions:
            policy_definitions[policy_name] = {}
        policy_definitions[policy_name][setting_name] = value
    # Read and process the list of policies to load.
    # We build up a list of callable which can be executed at config commit
    # time to obtain the final list of policies.
    policy_factories = []
    policy_names = settings.get("multiauth.policies", "").split()
    for policy_name in reversed(policy_names):
        if policy_name in policy_definitions:
            # It's a policy defined using a callable.
            # Just append it straight to the list.
            definition = policy_definitions[policy_name]
            factory = config.maybe_dotted(definition.pop("use"))
            policy_factories.append((factory, definition))
        else:
            # It's a module to be directly included.
            factory = policy_factory_from_include(config, policy_name)
            policy_factories.append((factory, {}))
    # OK.  We now have a list of callbacks which need to be called at
    # commit time, and will return the policies in reverse order.
    # Register a special action to pull them into our list of policies.
    policies = []
    authn_policy = MultiAuthenticationPolicy(policies, groupfinder)
    config.set_authentication_policy(authn_policy)
    def grab_policies():  # NOQA
        for factory, kwds in policy_factories:
            policy = factory(**kwds)
            if policy:
                if not policies or policy is not policies[0]:
                    policies.insert(0, policy)
    config.action(None, grab_policies, order=PHASE2_CONFIG)
    # Also hook up a default AuthorizationPolicy.
    # ACLAuthorizationPolicy is usually what you want.
    # If the app configures one explicitly then this will get overridden.
    authz_policy = ACLAuthorizationPolicy()
    config.set_authorization_policy(authz_policy)


def policy_factory_from_include(config, module):
    """Create a policy factory by processing a configuration include.

    This function does some trickery with the Pyramid config system.
    It does config.include(module), and them sucks out information about
    the authn policy that was registered.
    """
    # Remember the active policy before including the module, if any.
    orig_policy = config.registry.queryUtility(IAuthenticationPolicy)
    # Include the module, so we get any default views etc.
    config.include(module)
    # That might have registered and commited a new policy object.
    policy = config.registry.queryUtility(IAuthenticationPolicy)
    if policy is not None and policy is not orig_policy:
        return lambda: policy
    # Or it might have set up a pending action to register one later.
    # Find the most recent IAuthenticationPolicy action, and grab
    # out the registering function so we can call it ourselves.
    for action in reversed(config.action_state.actions):
        if action[0] is not IAuthenticationPolicy:
            continue
        def grab_policy(register=action[1]):  # NOQA
            register()
            return config.registry.queryUtility(IAuthenticationPolicy)
        return grab_policy
    # Or it might not have done *anything*.
    return lambda: None
