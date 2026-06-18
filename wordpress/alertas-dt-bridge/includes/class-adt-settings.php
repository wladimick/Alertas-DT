<?php
defined( 'ABSPATH' ) || exit;

class ADT_Settings {

    public static function generate_token(): string {
        return 'adt_' . bin2hex( random_bytes( 24 ) );
    }

    public static function get_token(): string {
        return (string) get_option( 'adt_api_token', '' );
    }

    public static function regenerate_token(): string {
        $token = self::generate_token();
        update_option( 'adt_api_token', $token, false );
        return $token;
    }

    public static function get_last_sync(): string {
        return (string) get_option( 'adt_last_sync', '' );
    }

    public static function set_last_sync( string $dt ): void {
        update_option( 'adt_last_sync', $dt );
    }

    public static function verify_token( string $token ): bool {
        $stored = self::get_token();
        return $stored && hash_equals( $stored, $token );
    }

    /**
     * Extract Bearer token from Authorization header or X-Alertas-DT-Token header.
     */
    public static function token_from_request( WP_REST_Request $request ): string {
        $auth = $request->get_header( 'authorization' );
        if ( $auth && str_starts_with( $auth, 'Bearer ' ) ) {
            return trim( substr( $auth, 7 ) );
        }
        return (string) $request->get_header( 'x-alertas-dt-token' );
    }
}
