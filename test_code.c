/* ssl/t1_srvr.c */

#include <stdio.h>
#include "ssl_locl.h"
#include <openssl/buffer.h>
#include <openssl/rand.h>
#include <openssl/objects.h>
#include <openssl/evp.h>
#include <openssl/x509.h>

static const SSL_METHOD *tls1_get_server_method(int ver);
static const SSL_METHOD *tls1_get_server_method(int ver)
{
    if (ver == TLS1_2_VERSION)
        return TLSv1_2_server_method();
    if (ver == TLS1_1_VERSION)
        return TLSv1_1_server_method();
    if (ver == TLS1_VERSION)
        return TLSv1_server_method();
    return NULL;
}

IMPLEMENT_tls_meth_func(TLS1_2_VERSION, TLSv1_2_server_method,
                        ssl3_accept,
                        ssl_undefined_function, tls1_get_server_method)

    IMPLEMENT_tls_meth_func(TLS1_1_VERSION, TLSv1_1_server_method,
                        ssl3_accept,
                        ssl_undefined_function, tls1_get_server_method)

    IMPLEMENT_tls_meth_func(TLS1_VERSION, TLSv1_server_method,
                        ssl3_accept,
                        ssl_undefined_function, tls1_get_server_method)                       
