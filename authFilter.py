from cgi import parse_qs
import base64
import json

class AuthFilter(object):
    """
    Simple MapProxy authorization middleware.

    It blocks wms request unless valid wms domain is specified in jwt.
    """
    def __init__(self, app,validDomain,autHeaderName = None, authQueryName = 'token'):
        self.app = app
        self.autHeaderName = autHeaderName.lower() if (autHeaderName != None) else None
        self.authQueryName = authQueryName.lower() if (authQueryName != None) else None
        self.validDomain = validDomain

    def __call__(self, environ, start_response):
        # put authorize callback function into environment
        environ['mapproxy.authorize'] = self.authorize
        return self.app(environ, start_response)

    def authorize(self, service, layers=[], environ=None, **kw):
        if service.startswith('wms.'):
            token = environ.get(f'HTTP_{self.autHeaderName}') if(self.autHeaderName) else None
            if(token == None and self.authQueryName):
                query = parse_qs(environ['QUERY_STRING'])
                token = query.get(self.authQueryName,[None])[0]
            if(token):
                try:
                    pyload = token.split('.')
                    pyload = base64.urlsafe_b64decode(pyload + '=' * (4 - len(pyload) % 4))
                    pyload = json.loads(pyload)
                    domains = pyload['d']
                    if self.validDomain in domains:
                        #allow authorized wms
                        return {'authorized': 'full'}
                except:
                    pass
            # block wms
            return {'authorized': 'none'}
        # allow everything that isn't blocked
        return {'authorized': 'full'}