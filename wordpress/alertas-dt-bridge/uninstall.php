<?php
// Runs when admin clicks "Delete" in WP plugins list.
defined( 'WP_UNINSTALL_PLUGIN' ) || exit;

global $wpdb;
$table = $wpdb->prefix . 'alertas_dt_subscribers';
// phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
$wpdb->query( "DROP TABLE IF EXISTS {$table}" );

delete_option( 'adt_api_token' );
delete_option( 'adt_last_sync' );
delete_option( 'adt_plugin_version' );
