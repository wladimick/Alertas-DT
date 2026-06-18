<?php
defined( 'ABSPATH' ) || exit;

class ADT_Activator {

    public static function activate(): void {
        ADT_Database::create_table();

        if ( ! get_option( 'adt_api_token' ) ) {
            update_option( 'adt_api_token', ADT_Settings::generate_token(), false );
        }

        update_option( 'adt_plugin_version', ADT_VERSION );
        flush_rewrite_rules();
    }

    public static function deactivate(): void {
        flush_rewrite_rules();
    }
}
